(function () {
  "use strict";
  if (window.__deepseekMonitorContentLoaded) return;
  window.__deepseekMonitorContentLoaded = true;

  const Core = globalThis.DeepSeekMonitorCore;
  let runnerActive = false;
  let currentSources = [];
  let networkSourceCount = 0;

  window.addEventListener("deepseek-monitor:sources", (event) => {
    const detail = event.detail || {};
    if (detail.reset) {
      currentSources = [];
      networkSourceCount = 0;
      return;
    }
    const added = (detail.items || []).map((item) => ({ ...item, via: item.via || "network" }));
    networkSourceCount += added.length;
    currentSources = Core.dedupeSources(currentSources.concat(added));
  });

  function message(payload) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(payload, (response) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(response || {});
      });
    });
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 2 && box.height > 2;
  };
  const label = (element) => Core.normalizeText([
    element.innerText, element.textContent, element.getAttribute("aria-label"),
    element.getAttribute("title"), element.getAttribute("data-testid")
  ].filter(Boolean).join(" "));

  function allClickable() {
    return Array.from(document.querySelectorAll("button,a,[role='button'],[tabindex],div[class*='button']")).filter(visible);
  }

  function findNewChatButton() {
    const patterns = [/^\+?\s*新对话$/i, /^\+?\s*新建对话$/i, /new\s*chat/i, /start\s*new\s*chat/i];
    return allClickable()
      .map((element) => {
        const text = label(element);
        const box = element.getBoundingClientRect();
        let score = patterns.some((pattern) => pattern.test(text)) ? 100 : 0;
        if (/新(建)?对话|new\s*chat/i.test(text)) score += 40;
        if (box.left < innerWidth * 0.3) score += 20;
        if (box.top < innerHeight * 0.3) score += 10;
        if (text.length > 40) score -= 50;
        return { element, score };
      })
      .filter((item) => item.score >= 60)
      .sort((a, b) => b.score - a.score)[0]?.element || null;
  }

  function isFreshChatPage() {
    const path = location.pathname.replace(/\/+$/, "") || "/";
    const hasMessages = Boolean(document.querySelector(
      ".ds-virtual-list-visible-items .ds-message,.ds-assistant-message-main-content,[data-message-id]"
    ));
    return path === "/" && Boolean(findComposer()) && !hasMessages;
  }

  function canCreateNewChat() {
    return location.hostname === "chat.deepseek.com" && Boolean(findComposer());
  }

  function composerScore(element) {
    if (!visible(element) || element.disabled || element.readOnly) return -Infinity;
    const box = element.getBoundingClientRect();
    const minimumWidth = Math.min(280, innerWidth * 0.28);
    const inMainArea = box.right >= innerWidth * 0.42 && box.bottom >= innerHeight * 0.2;
    if (box.width < minimumWidth || !inMainArea) return -Infinity;
    const text = [element.getAttribute("placeholder"), element.getAttribute("aria-label"), element.getAttribute("data-placeholder")].filter(Boolean).join(" ");
    let score = 0;
    if (/提问|问问|输入|发送|message|ask|chat/i.test(text)) score += 50;
    if (element.matches("textarea")) score += 25;
    if (element.matches("[contenteditable='true']")) score += 20;
    if (box.top > innerHeight * 0.55) score += 25;
    if (box.width > Math.min(420, innerWidth * 0.35)) score += 20;
    if (box.height > 160) score -= 15;
    return score;
  }

  function findComposer() {
    const candidates = Array.from(document.querySelectorAll(
      "textarea,[contenteditable='true'],input[type='text'],div[role='textbox']"
    ));
    return candidates.map((element) => ({ element, score: composerScore(element) }))
      .filter((item) => Number.isFinite(item.score) && item.score >= 40)
      .sort((a, b) => b.score - a.score)[0]?.element || null;
  }

  function composerValue(element) {
    if (!element) return "";
    return Core.normalizeText("value" in element ? element.value : element.innerText || element.textContent || "");
  }

  async function waitForComposer(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const composer = findComposer();
      if (composer) return composer;
      await sleep(500);
    }
    const error = retryableError("DeepSeek新对话页面尚未加载出可用输入框", "DM_COMPOSER_NOT_READY");
    error.reloadPage = true;
    throw error;
  }

  async function createNewChat() {
    if (isFreshChatPage()) {
      await logEvent("new_chat_ready", "info", { page_url: location.href, method: "fresh_home" });
      return waitForComposer(15000);
    }
    const button = findNewChatButton();
    if (!button) {
      await logEvent("new_chat_navigate", "info", { previous_url: location.href });
      location.assign("https://chat.deepseek.com/");
      await new Promise(() => {});
    }
    await logEvent("new_chat_click", "info", { previous_url: location.href });
    const previousConversationId = Core.conversationIdFromUrl(location.href);
    button.click();
    const transitionDeadline = Date.now() + 15000;
    while (Date.now() < transitionDeadline) {
      const currentConversationId = Core.conversationIdFromUrl(location.href);
      if (
        isFreshChatPage()
        && (!previousConversationId || currentConversationId !== previousConversationId)
      ) break;
      await sleep(250);
    }
    if (!isFreshChatPage()) {
      await logEvent("new_chat_transition_failed", "warning", {
        previous_url: location.href,
        previous_conversation_id: previousConversationId,
        current_conversation_id: Core.conversationIdFromUrl(location.href),
        old_messages_visible: Boolean(document.querySelector(
          ".ds-virtual-list-visible-items .ds-message,.ds-assistant-message-main-content,[data-message-id]"
        ))
      });
      const error = retryableError(
        "点击新对话后旧会话内容没有完全清空；本轮未发送，将在下一个发送窗口换新会话重试",
        "DM_NEW_CHAT_NOT_FRESH"
      );
      error.reloadPage = true;
      throw error;
    }
    await logEvent("new_chat_verified", "info", {
      previous_conversation_id: previousConversationId,
      page_url: location.href,
      old_messages_visible: false
    });
    const composer = await waitForComposer(5000);
    const deadline = Date.now() + 2500;
    while (Date.now() < deadline && composerValue(composer)) await sleep(250);
    if (composerValue(composer)) {
      const draftLength = composerValue(composer).length;
      clearComposerValue(composer);
      await sleep(500);
      await logEvent("old_draft_cleared", "info", { draft_length: draftLength });
    }
    if (composerValue(composer)) {
      const error = retryableError("点击新对话后旧草稿自动清空失败", "DM_DRAFT_CLEAR_FAILED");
      error.reloadPage = true;
      throw error;
    }
    return composer;
  }

  async function ensureSmartSearch() {
    const searchLabel = /^(智能搜索|联网搜索|搜索|search|web search)$/i;
    const candidates = Array.from(document.querySelectorAll("button,[role='button'],[aria-pressed],.ds-toggle-button,[tabindex]"))
      .filter((element) => visible(element) && searchLabel.test(Core.normalizeText(
        element.innerText || element.textContent || element.getAttribute("aria-label") || element.getAttribute("title") || ""
      )));
    const toggle = candidates.sort((a, b) => a.childElementCount - b.childElementCount)[0];
    if (!toggle) throw retryableError("未找到“Search/智能搜索”开关，无法保证抓取信源", "DM_SEARCH_TOGGLE_NOT_READY");
    if (toggle.getAttribute("aria-pressed") !== "true" && !toggle.classList.contains("ds-toggle-button--selected")) {
      toggle.click();
      await sleep(500);
    }
    if (toggle.getAttribute("aria-pressed") !== "true" && !toggle.classList.contains("ds-toggle-button--selected")) {
      throw retryableError("“Search/智能搜索”未能开启，已停止本轮发送", "DM_SEARCH_TOGGLE_FAILED");
    }
  }

  async function waitForSendSlot() {
    const slot = await message({ type: "CLAIM_SEND_SLOT" });
    if (slot.ok) return slot;
    const waitMs = Math.max(1000, Number(slot.waitMs) || 1000);
    const wakeAt = Number(slot.nextAllowedSendAt) || Date.now() + waitMs;
    await logEvent("rate_limit_wait", "info", {
      wait_seconds: Math.ceil(waitMs / 1000), next_allowed_send_at: new Date(wakeAt).toISOString(),
      wait_mode: "extension_alarm"
    });
    await message({ type: "SCHEDULE_RUNNER_WAKE", when: wakeAt });
    return null;
  }

  function clearComposerValue(element) {
    element.focus();
    try {
      element.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true, cancelable: true, inputType: "deleteContentBackward", data: null
      }));
    } catch (_) {}
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, "");
      else element.value = "";
    } else {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
      try { document.execCommand("delete", false); } catch (_) { element.textContent = ""; }
      if (Core.compact(composerValue(element))) element.textContent = "";
    }
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "deleteContentBackward", data: null
    }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setComposerValue(element, value) {
    element.focus();
    try {
      element.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: value }));
    } catch (_) {}
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, value);
      else element.value = value;
    } else {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
      try { document.execCommand("delete", false); } catch (_) {}
      try { document.execCommand("insertText", false, value); } catch (_) { element.textContent = value; }
      if (!Core.compact(composerValue(element))) element.textContent = value;
    }
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function findSendButton(composer) {
    const composerBox = composer.getBoundingClientRect();
    const regions = [];
    let parent = composer.parentElement;
    for (let i = 0; parent && i < 6; i += 1, parent = parent.parentElement) regions.push(parent);
    const raw = regions.flatMap((region) => Array.from(region.querySelectorAll(
      "button,[role='button'],[tabindex],[class*='send'],[class*='submit'],[data-testid],svg"
    )));
    const candidates = Array.from(new Set(raw.map((element) => {
      if (element.tagName.toLowerCase() !== "svg") return element;
      return element.closest("button,[role='button'],[tabindex],[class*='send'],[class*='submit']") || element.parentElement;
    }).filter(Boolean))).filter((element) => visible(element) && !element.disabled &&
      element.getAttribute("aria-disabled") !== "true" && !element.classList.contains("ds-button--disabled"));
    return candidates.map((element) => {
      const text = label(element) + " " + String(element.className?.baseVal || element.className || "") + " " + String(element.innerHTML || "").slice(0, 300);
      const box = element.getBoundingClientRect();
      const horizontallyNear = box.right >= composerBox.left - 40 && box.left <= composerBox.right + 140;
      const verticallyNear = box.bottom >= composerBox.top - 80 && box.top <= composerBox.bottom + 80;
      if (!horizontallyNear || !verticallyNear) return { element, score: -Infinity };
      let score = 0;
      if (/^发送$|发送消息|send/i.test(text)) score += 100;
      if (element.classList.contains("ds-button--primary") && element.classList.contains("ds-button--circle")) score += 80;
      if (/arrow|submit|send/i.test(element.className || "")) score += 40;
      if (box.left >= composerBox.left + composerBox.width * 0.65) score += 20;
      if (Math.abs(box.bottom - composerBox.bottom) < 100) score += 20;
      if (box.width <= 80 && box.height <= 80) score += 10;
      if (getComputedStyle(element).cursor === "pointer") score += 12;
      score += Math.max(0, Math.min(20, (box.left - composerBox.left) / Math.max(1, composerBox.width) * 20));
      if (/语音|麦克风|录音|附件|上传|voice|microphone|upload/i.test(text)) score -= 120;
      return { element, score };
    }).filter((item) => item.score >= 35).sort((a, b) => b.score - a.score)[0]?.element || null;
  }

  function dispatchEnter(composer) {
    composer.focus();
    for (const type of ["keydown", "keypress", "keyup"]) {
      composer.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13,
        bubbles: true, cancelable: true, composed: true
      }));
    }
  }

  function sendDiagnostics(composer) {
    const box = composer.getBoundingClientRect();
    const nearby = Array.from(document.querySelectorAll("button,[role='button'],[tabindex],[class*='send'],[class*='submit']"))
      .filter(visible)
      .map((element) => {
        const itemBox = element.getBoundingClientRect();
        return { element, itemBox, distance: Math.abs(itemBox.bottom - box.bottom) + Math.abs(itemBox.right - box.right) };
      })
      .sort((a, b) => a.distance - b.distance)
      .slice(0, 8)
      .map(({ element, itemBox }) => ({
        tag: element.tagName.toLowerCase(), text: label(element).slice(0, 80),
        class: String(element.className?.baseVal || element.className || "").slice(0, 120),
        disabled: Boolean(element.disabled), ariaDisabled: element.getAttribute("aria-disabled"),
        x: Math.round(itemBox.x), y: Math.round(itemBox.y), width: Math.round(itemBox.width), height: Math.round(itemBox.height)
      }));
    return JSON.stringify({ composer: {
      tag: composer.tagName.toLowerCase(), role: composer.getAttribute("role"),
      contenteditable: composer.getAttribute("contenteditable"), class: String(composer.className || "").slice(0, 160),
      x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height)
    }, nearby });
  }

  function submissionEvidence(prompt, initialUrl) {
    const initialConversationId = Core.conversationIdFromUrl(initialUrl);
    const currentConversationId = Core.conversationIdFromUrl(location.href);
    const conversationCreated = Boolean(currentConversationId && currentConversationId !== initialConversationId);
    const promptMessage = findPromptElement(prompt);
    const currentComposer = findComposer();
    const draftCleared = !currentComposer || !Core.compact(composerValue(currentComposer));
    if (promptMessage && draftCleared) return {
      confirmed: true, method: "prompt_message_and_cleared", currentConversationId,
      conversationCreated, draftCleared, promptMessage: true
    };
    if (promptMessage && generationBusy()) return {
      confirmed: true, method: "prompt_message_and_generation", currentConversationId,
      conversationCreated, draftCleared, promptMessage: true
    };
    if (conversationCreated && generationBusy()) return {
      confirmed: true, method: "new_conversation_and_generation", currentConversationId,
      conversationCreated, draftCleared, promptMessage: Boolean(promptMessage)
    };
    return {
      confirmed: false, method: "", currentConversationId, conversationCreated,
      draftCleared, promptMessage: Boolean(promptMessage)
    };
  }

  async function submitPrompt(composer, prompt) {
    setComposerValue(composer, prompt);
    await sleep(800);
    if (Core.compact(composerValue(composer)) !== Core.compact(prompt)) {
      const error = retryableError("问题写入输入框后校验失败", "DM_COMPOSER_WRITE_FAILED");
      error.reloadPage = true;
      throw error;
    }
    await logEvent("prompt_prepared", "info", { prompt, composer: sendDiagnostics(composer) });
    let activeComposer = composer;
    let send = null;
    const sendReadyDeadline = Date.now() + 6000;
    while (Date.now() < sendReadyDeadline) {
      const current = findComposer();
      if (current && current !== activeComposer) {
        activeComposer = current;
        if (Core.compact(composerValue(activeComposer)) !== Core.compact(prompt)) {
          setComposerValue(activeComposer, prompt);
          await sleep(400);
        }
      }
      send = findSendButton(activeComposer);
      if (send) break;
      await sleep(400);
    }
    const initialUrl = location.href;
    if (send) send.click();
    else dispatchEnter(activeComposer);
    const deadline = Date.now() + 30000;
    let lastEvidence = null;
    let promptStableSince = 0;
    while (Date.now() < deadline) {
      const evidence = submissionEvidence(prompt, initialUrl);
      lastEvidence = evidence;
      if (evidence.promptMessage && evidence.draftCleared) {
        if (!promptStableSince) promptStableSince = Date.now();
      } else {
        promptStableSince = 0;
      }
      if (evidence.confirmed) {
        // A cleared draft plus a briefly rendered prompt is not sufficient:
        // DeepSeek sometimes creates a titled route and then removes the user
        // message without starting generation. Require the prompt to survive
        // several render cycles unless generation is visibly active.
        const weakPromptOnly = evidence.method === "prompt_message_and_cleared";
        if (weakPromptOnly && Date.now() - promptStableSince < 5000) {
          await sleep(500);
          continue;
        }
        await logEvent("prompt_submitted", "info", {
          prompt, page_url: location.href, confirmation_method: evidence.method,
          conversation_id: evidence.currentConversationId
        });
        return evidence;
      }
      await sleep(500);
    }
    const diagnostic = sendDiagnostics(activeComposer);
    if (lastEvidence?.conversationCreated && !lastEvidence.promptMessage) {
      await logEvent("silent_submission_detected", "warning", {
        prompt, page_url: location.href, conversation_id: lastEvidence.currentConversationId,
        prompt_visible: false, generation_busy: generationBusy(), diagnostic
      });
      // DeepSeek sometimes creates the conversation immediately but hydrates
      // the prompt/answer several seconds later.  Once a new conversation id
      // exists the send is ambiguous, not safely repeatable: treating it as a
      // failed send produced duplicate conversations for the same question.
      // Persist this conversation as submitted and only retry reading it.
      await logEvent("prompt_submitted", "warning", {
        prompt, page_url: location.href, confirmation_method: "new_conversation_ambiguous",
        conversation_id: lastEvidence.currentConversationId
      });
      return {
        ...lastEvidence, confirmed: true, method: "new_conversation_ambiguous"
      };
    }
    await logEvent("send_unconfirmed", "warning", { prompt, page_url: location.href, diagnostic });
    const error = retryableError("问题已写入，但DeepSeek页面没有确认发送；将刷新页面后重试", "DM_SUBMIT_UNCONFIRMED");
    error.reloadPage = true;
    throw error;
  }

  function findPromptElement(prompt) {
    const target = Core.compact(prompt);
    if (!target) return null;
    const nodes = Array.from(document.querySelectorAll(".ds-message,p,div,span,[data-message-id],[class*='message']"));
    return nodes.filter((element) => {
      if (!visible(element) || element.closest("aside,nav,header")) return false;
      const box = element.getBoundingClientRect();
      // Exclude the left conversation list. Its generated title can resemble
      // the prompt and previously caused a false submission confirmation.
      return box.right > Math.max(320, innerWidth * 0.30);
    }).map((element) => ({
      element,
      text: Core.compact(element.innerText || element.textContent || "")
    })).filter((item) => item.text === target || (item.text.startsWith(target) && item.text.length < target.length + 30))
      .sort((a, b) => (a.element.childElementCount - b.element.childElementCount))[0]?.element || null;
  }

  function follows(element, reference) {
    if (!reference || !element || element === reference || element.contains(reference)) return false;
    return Boolean(reference.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING);
  }

  function answerText(element) {
    if (!element) return "";
    const raw = String(element.innerText || element.textContent || "");
    return Core.normalizeText(raw.replace(/-\s*\n\s*\d{1,3}(?=\s*(?:\n|$))/g, ""));
  }

  function answerCandidates(prompt) {
    const promptElement = findPromptElement(prompt);
    const nodes = new Set(Array.from(document.querySelectorAll(
      ".ds-assistant-message-main-content,[data-message-id],[data-role*='assistant'],[class*='assistant'],[class*='answer'],[class*='response'],[class*='message'],[class*='markdown'],[class*='content']"
    )));
    if (promptElement) {
      const fallbackNodes = Array.from(document.querySelectorAll(
        "main article,main section,main div,article,section,[role='article'],[role='region']"
      )).slice(0, 8000);
      for (const element of fallbackNodes) {
        if (!visible(element) || !follows(element, promptElement)) continue;
        const text = answerText(element);
        if (text.length < 60 || text.length > 30000) continue;
        if (/直接提问|新对话/.test(text) || element.querySelector("textarea,[contenteditable='true'],div[role='textbox']")) continue;
        nodes.add(element);
      }
    }
    for (const button of allClickable().filter((item) => /复制|copy/i.test(label(item)))) {
      let parent = button.parentElement;
      for (let i = 0; parent && i < 6; i += 1, parent = parent.parentElement) {
        const length = answerText(parent).length;
        if (length >= 30 && length <= 100000) nodes.add(parent);
      }
    }
    const items = [];
    for (const element of nodes) {
      if (!visible(element)) continue;
      const text = answerText(element);
      if (text.length < 20 || text.length > 120000) continue;
      const box = element.getBoundingClientRect();
      const classText = String(element.className || "");
      items.push({
        element,
        text,
        afterPrompt: follows(element, promptElement),
        hasCopyAction: Array.from(element.querySelectorAll("button,[role='button']")).some((item) => /复制|copy/i.test(label(item))),
        hasCitation: Boolean(element.querySelector("sup,[class*='cite'],[class*='reference'],a[href]")),
        isMessageLike: element.matches(".ds-assistant-message-main-content") || /assistant|answer|response|message|markdown/i.test(classText),
        isRootLike: element === document.body || element.matches("main,[id='app'],[id='root']") || text.length > 50000,
        containsSidebar: /新对话/.test(text) && box.left < innerWidth * 0.25,
        selectorHint: [element.tagName.toLowerCase(), element.id ? "#" + element.id : "", classText ? "." + classText.split(/\s+/).slice(0, 3).join(".") : ""].join("")
      });
    }
    return Core.chooseAnswerCandidate(items, prompt);
  }

  function generationBusy() {
    if (document.querySelector(".ds-icon-button--rotating,[data-testid*='stop']")) return true;
    return allClickable().some((element) => /停止(生成|回答|响应)|stop\s*(generating|answer|response)|取消生成/i.test(label(element)));
  }

  function retryableError(message, code) {
    const error = new Error(message);
    error.code = code || "DM_TRANSIENT";
    error.retryable = true;
    return error;
  }

  async function waitForAnswer(prompt, settings, submittedAt, recoveryAttempt = 0) {
    const timeoutMs = Math.max(30, Number(settings.timeoutSeconds) || 240) * 1000;
    const stableNeeded = Math.max(3, Math.ceil((Number(settings.stableSeconds) || 10) / 2));
    const deadline = Date.now() + timeoutMs;
    let last = "";
    let stable = 0;
    let contentStable = 0;
    let bestCandidate = null;
    let lastDiagnosticAt = submittedAt || Date.now();
    let lastBusy = false;
    let firstIdleEmptyAt = 0;
    const waitStartedAt = Date.now();
    let sawPrompt = false;
    let sawCandidate = false;
    let sawGeneration = false;
    while (Date.now() < deadline) {
      const control = await message({ type: "GET_JOB" });
      if (!control.job || ["stopped", "error"].includes(control.job.state)) throw new Error("任务已停止");
      while (control.job?.state === "paused") {
        await sleep(1000);
        const again = await message({ type: "GET_JOB" });
        if (again.job?.state !== "paused") break;
      }
      const bodyText = document.body.innerText || "";
      const pageFailure = Core.detectTransientFailure(bodyText);
      if (pageFailure && Date.now() - submittedAt >= 3000) {
        throw retryableError(`DeepSeek页面返回“${pageFailure}”`, "DM_PAGE_TRANSIENT");
      }
      const blockingState = Core.detectBlockingState(bodyText);
      if (blockingState && Date.now() - submittedAt >= 3000) {
        const error = retryableError(`DeepSeek页面提示“${blockingState}”`, "DM_PAGE_BLOCKED");
        error.reloadPage = true;
        error.pauseJob = true;
        throw error;
      }
      const candidate = answerCandidates(prompt);
      const text = candidate?.text || "";
      const busy = generationBusy();
      const promptVisible = Boolean(findPromptElement(prompt));
      sawPrompt = sawPrompt || promptVisible;
      sawCandidate = sawCandidate || Boolean(candidate);
      sawGeneration = sawGeneration || busy;
      const minimumLength = Math.max(30, Number(settings.minAnswerLength) || 60);
      const equivalent = text.length >= minimumLength && Core.answerTextEquivalent(text, last);
      contentStable = equivalent ? contentStable + 1 : 0;
      stable = !busy && equivalent ? stable + 1 : 0;
      if (candidate && text.length >= minimumLength && (!bestCandidate || text.length >= bestCandidate.text.length)) {
        bestCandidate = candidate;
      }
      last = text;
      lastBusy = busy;
      if (!busy && !candidate) {
        if (!firstIdleEmptyAt) firstIdleEmptyAt = Date.now();
      } else {
        firstIdleEmptyAt = 0;
      }
      const idleEmptyMs = firstIdleEmptyAt ? Date.now() - firstIdleEmptyAt : 0;
      const submittedElapsed = Date.now() - submittedAt;
      // On the first pass only, one early reload is useful when DeepSeek has
      // created the conversation route but has not hydrated its messages yet.
      // After that reload we must wait the full configured answer timeout.
      // Repeating the old 20-second shortcut on every recovery pass caused a
      // real, delayed answer to be recorded as a failed/empty round.
      const firstPassHydrationMs = Math.min(timeoutMs, 60000);
      if (
        recoveryAttempt === 0
        && !promptVisible
        && idleEmptyMs >= firstPassHydrationMs
        && submittedElapsed >= firstPassHydrationMs
      ) {
        const error = retryableError(
          "DeepSeek新会话在60秒内尚未加载出问题或回答；刷新后将按完整回答超时重读原会话，不重复提问",
          "DM_SUBMISSION_LOST"
        );
        error.reloadPage = true;
        error.preserveSubmission = true;
        throw error;
      }
      // A route/title alone is not proof that DeepSeek accepted the message.
      // After one reload, if the original conversation still never exposes a
      // user prompt, generation state, or answer, it is a confirmed empty
      // shell. Release only that empty submission so the same round can be
      // sent again at the next normal six-minute slot.
      const emptyRecoveryMs = Math.min(timeoutMs, 120000);
      if (
        recoveryAttempt > 0
        && !sawPrompt
        && !sawCandidate
        && !sawGeneration
        && Date.now() - waitStartedAt >= emptyRecoveryMs
      ) {
        const error = retryableError(
          "DeepSeek原会话刷新后仍是空会话；将在下一个6分钟发送窗口重新发送本轮问题",
          "DM_EMPTY_CONVERSATION"
        );
        error.reloadPage = true;
        error.preserveSubmission = false;
        throw error;
      }
      const firstPassGenerationMs = Math.min(timeoutMs, 90000);
      if (
        recoveryAttempt === 0
        && promptVisible
        && idleEmptyMs >= firstPassGenerationMs
        && submittedElapsed >= firstPassGenerationMs
      ) {
        const error = retryableError(
          "DeepSeek已显示问题但90秒内未识别到回答；刷新后将按完整回答超时重读原会话，不重复提问",
          "DM_GENERATION_NOT_STARTED"
        );
        error.reloadPage = true;
        error.preserveSubmission = true;
        throw error;
      }
      if (stable >= stableNeeded && Date.now() - submittedAt >= 8000) {
        await logEvent("answer_stable", "info", { prompt, reply_chars: text.length, stable_polls: stable });
        return { ...candidate, completionMode: "stable" };
      }
      const stuckThreshold = Math.max(12, stableNeeded * 3);
      if (contentStable >= stuckThreshold && Date.now() - submittedAt >= 45000) {
        await logEvent("answer_ui_state_fallback", "warning", {
          prompt, reply_chars: text.length, stable_polls: contentStable,
          generation_busy: busy, selector_hint: candidate?.selectorHint || ""
        });
        return { ...candidate, completionMode: "ui_state_fallback" };
      }
      if (Date.now() - lastDiagnosticAt >= 60000) {
        lastDiagnosticAt = Date.now();
        await logEvent("answer_wait_diagnostic", "info", {
          prompt, reply_chars: text.length, stable_polls: contentStable,
          generation_busy: busy, has_candidate: Boolean(candidate), selector_hint: candidate?.selectorHint || ""
        });
      }
      await sleep(2000);
    }
    if (bestCandidate?.text?.length >= Math.max(30, Number(settings.minAnswerLength) || 60)) {
      await logEvent("answer_timeout_fallback", "warning", {
        prompt, reply_chars: bestCandidate.text.length, stable_polls: contentStable,
        generation_busy: lastBusy, selector_hint: bestCandidate.selectorHint || ""
      });
      return { ...bestCandidate, completionMode: "timeout_fallback" };
    }
    const error = retryableError(`已等待完整的回答超时时间，但原会话仍没有可保存的有效正文（候选 ${bestCandidate?.text?.length || 0} 字）`, "DM_ANSWER_TIMEOUT");
    error.reloadPage = true;
    // The send was already confirmed. Retrying it would create a duplicate
    // conversation, so all recovery after this point stays on the same URL.
    error.preserveSubmission = true;
    throw error;
  }

  function sourceElements(root) {
    const scope = root?.element || document;
    const anchors = Array.from(scope.querySelectorAll("a[href],[data-href],[data-url]"));
    const overlays = Array.from(document.querySelectorAll(
      "[role='tooltip'] a[href],[role='dialog'] a[href],[class*='popover'] a[href],[class*='tooltip'] a[href]," +
      "[class*='reference'] a[href],[class*='source'] a[href],[class*='card'] a[href]"
    ));
    const visibleExternal = Array.from(document.querySelectorAll("a[href],[data-href],[data-url]"))
      .filter((element) => visible(element) && Core.isExternalUrl(element.href || element.getAttribute("data-href") || element.getAttribute("data-url")));
    return Array.from(new Set(anchors.concat(overlays, visibleExternal)));
  }

  function sourceCardText(element) {
    // Inline citations are nested in the assistant paragraph. Walking upward
    // from the link therefore turned the answer sentence into a fake page
    // title. Trust only explicit title nodes/attributes and the link's own
    // first meaningful line; an unknown title is safer than fabricated data.
    const explicit = element.querySelector?.(
      ".search-view-card__title,[data-title],[class*='result-title'],[class*='card-title'],h1,h2,h3,h4"
    );
    if (explicit) return explicit.getAttribute("data-title") || explicit.innerText || explicit.textContent || "";
    for (const value of [element.getAttribute("data-title"), element.getAttribute("title"), element.getAttribute("aria-label")]) {
      if (Core.normalizeText(value).length >= 3) return value;
    }
    const lines = Core.normalizeText(element.innerText || element.textContent || "").split("\n")
      .map((line) => line.trim()).filter(Boolean);
    return lines.find((line) => line.length >= 3 && !/^\D*\d{1,3}\D*$/.test(line)) || "";
  }

  function collectDomSources(candidate) {
    return Core.dedupeSources(sourceElements(candidate).map((element) => ({
      url: element.href || element.getAttribute("data-href") || element.getAttribute("data-url"),
      title: Core.pickSourceTitle(
        element.querySelector?.(".search-view-card__title")?.innerText || sourceCardText(element),
        element.href || element.getAttribute("data-href") || element.getAttribute("data-url")
      ),
      via: "dom"
    })));
  }

  function sourceSummaryControls(candidate) {
    const roots = [candidate?.element, document].filter(Boolean);
    const found = [];
    for (const root of roots) {
      for (const element of root.querySelectorAll("button,a,span,[role='button'],[tabindex]")) {
        if (!visible(element)) continue;
        const ownLabel = Core.normalizeText(element.innerText || element.textContent || element.getAttribute("aria-label") || "");
        const count = Core.sourceSummaryCount(ownLabel);
        if (!count) continue;
        const clickable = element.closest("button,a,[role='button'],[tabindex]") || element;
        const clickableLabel = Core.normalizeText(clickable.innerText || clickable.textContent || clickable.getAttribute("aria-label") || "");
        // The response wrapper can itself have role=button and contain the
        // source-summary text plus the whole answer. Clicking that wrapper did
        // nothing. Prefer the smallest exact descendant; its click bubbles to
        // the real source-summary handler.
        const target = ownLabel.length <= 100 ? element : clickable;
        const targetLabel = target === element ? ownLabel : clickableLabel;
        if (!found.some((item) => item.element === target)) {
          found.push({ element: target, count, label: targetLabel, compact: targetLabel.length <= 100 });
        }
      }
    }
    return found.sort((a, b) => {
      const aInside = candidate?.element?.contains(a.element) ? 1 : 0;
      const bInside = candidate?.element?.contains(b.element) ? 1 : 0;
      if (aInside !== bInside) return bInside - aInside;
      if (a.compact !== b.compact) return a.compact ? -1 : 1;
      if (a.label.length !== b.label.length) return a.label.length - b.label.length;
      return b.element.getBoundingClientRect().top - a.element.getBoundingClientRect().top;
    });
  }

  function searchResultsPanel() {
    const headings = Array.from(document.querySelectorAll("h1,h2,h3,[role='heading'],div,span"))
      .filter((element) => {
        const text = Core.normalizeText(element.textContent || "");
        return visible(element) && text.length <= 120 && /^(?:Search results|搜索结果|检索结果)\b/i.test(text);
      });
    for (const heading of headings) {
      let parent = heading;
      for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) {
        const box = parent.getBoundingClientRect();
        if (box.width >= 260 && box.height >= innerHeight * 0.45 && box.left >= innerWidth * 0.45) return parent;
      }
    }
    // DeepSeek occasionally renders the drawer title without a dedicated
    // heading node. Fall back to the large right-side container that owns the
    // external result links.
    const candidates = Array.from(document.querySelectorAll("aside,[role='dialog'],[class*='drawer'],[class*='panel'],div"))
      .filter((element) => {
        if (!visible(element)) return false;
        const box = element.getBoundingClientRect();
        if (box.left < innerWidth * 0.58 || box.width < 260 || box.height < innerHeight * 0.45) return false;
        const links = Array.from(element.querySelectorAll("a[href],[data-href],[data-url]"));
        return links.some((link) => Core.isExternalUrl(link.href || link.getAttribute("data-href") || link.getAttribute("data-url")));
      })
      .sort((a, b) => {
        const aa = a.getBoundingClientRect();
        const bb = b.getBoundingClientRect();
        return (bb.width * bb.height) - (aa.width * aa.height);
      });
    return candidates[0] || null;
  }

  async function scrollSourceControlIntoView(element) {
    // The source summary is below the answer. DeepSeek does not reliably open
    // it from an off-screen synthetic click, so first move every scrollable
    // ancestor (and the page) to the bottom, exactly like a user would.
    const ancestors = [];
    for (let parent = element?.parentElement; parent; parent = parent.parentElement) {
      if (parent.scrollHeight > parent.clientHeight + 8) ancestors.push(parent);
    }
    for (const parent of ancestors.reverse()) {
      parent.scrollTop = parent.scrollHeight;
      parent.dispatchEvent(new Event("scroll", { bubbles: true }));
    }
    window.scrollTo({ top: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight), behavior: "instant" });
    element.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    await sleep(700);
  }

  async function collectSearchResultsPanel(candidate, expected) {
    const controls = sourceSummaryControls(candidate);
    if (!controls.length) return [];
    const control = controls[0].element;
    await scrollSourceControlIntoView(control);
    await logEvent("source_drawer_click", "info", {
      label: controls[0].label || Core.normalizeText(control.innerText || control.textContent || control.getAttribute("aria-label") || ""),
      expected_source_count: expected
    });
    control.click();
    const deadline = Date.now() + 12000;
    let panel = null;
    while (Date.now() < deadline) {
      panel = searchResultsPanel();
      if (panel) break;
      await sleep(200);
    }
    if (!panel) throw retryableError("已滚动到底部并点击信源入口，但未识别到 Search results 侧栏", "DM_SOURCE_PANEL_NOT_READY");
    await logEvent("source_drawer_open", "info", { expected_source_count: expected });
    let collected = [];
    let unchanged = 0;
    for (let pass = 0; pass < 12; pass += 1) {
      const before = collected.length;
      collected = Core.dedupeSources(collected.concat(collectDomSources({ element: panel })));
      if (expected && collected.length >= expected) break;
      const scrollables = [panel, ...panel.querySelectorAll("div,section,aside")]
        .filter((element) => element.scrollHeight > element.clientHeight + 8)
        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
      const scroller = scrollables[0];
      if (!scroller) break;
      const previousTop = scroller.scrollTop;
      scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + Math.max(320, scroller.clientHeight * 0.8));
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      await sleep(250);
      unchanged = collected.length === before && scroller.scrollTop === previousTop ? unchanged + 1 : 0;
      if (unchanged >= 2) break;
    }
    return collected;
  }

  function citationMarkers(candidate) {
    if (!candidate?.element) return [];
    return Array.from(candidate.element.querySelectorAll("a[href],sup,button,[role='button'],[class*='cite'],[class*='reference'],[class*='source']"))
      .filter((element) => {
        const text = String(element.innerText || element.textContent || "").trim();
        return visible(element) && (/ds-markdown-cite/.test(String(element.className || "")) || /^\D*\d{1,3}\D*$/.test(text));
      })
      .slice(0, 60);
  }

  async function revealAndCollectSources(candidate) {
    let collected = collectDomSources(candidate);
    const controls = sourceSummaryControls(candidate);
    const expected = controls.reduce((value, item) => Math.max(value, item.count), 0);
    if (controls.length) {
      collected = Core.dedupeSources(collected.concat(await collectSearchResultsPanel(candidate, expected)));
    }
    if (!expected || collected.length < expected) for (const marker of citationMarkers(candidate).slice(0, 20)) {
      try {
        marker.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
        marker.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        marker.focus({ preventScroll: true });
      } catch (_) {}
      await sleep(180);
      collected = Core.dedupeSources(collected.concat(collectDomSources(candidate)));
    }
    return collected;
  }

  function expectedSourceCount(candidate) {
    const values = citationMarkers(candidate).map((element) => {
      const match = String(element.innerText || element.textContent || "").match(/\d{1,3}/);
      return match ? Number(match[0]) : 0;
    }).filter(Number.isFinite);
    const body = candidate?.rawText || candidate?.text || "";
    const prefixMatch = body.match(/(?:共参考|参考|信源|来源)\s*(\d{1,3})\s*(?:篇|条|个)?/);
    const suffixMatches = Array.from(body.matchAll(/(\d{1,3})\s*篇来源/g));
    const suffixCount = suffixMatches.length ? Number(suffixMatches[suffixMatches.length - 1][1]) : 0;
    const pageCounts = sourceSummaryControls(candidate).map((item) => item.count);
    return Math.max(prefixMatch ? Number(prefixMatch[1]) : 0, suffixCount, ...values, ...pageCounts, 0);
  }

  async function logEvent(event, level, details) {
    try {
      await message({ type: "LOG_EVENT", event, level: level || "info", details: details || {} });
    } catch (_) {}
  }

  function detectedModel() {
    const text = document.body.innerText || "";
    return text.match(/DeepSeek(?:[-\s]*(?:V\d+(?:\.\d+)?|R\d+(?:\.\d+)?|Chat|Reasoner))?/i)?.[0] || "DeepSeek";
  }

  async function processItem(job, settings) {
    const item = job.schedule[job.cursor];
    if (!item) return null;
    const prior = job.inFlight || {};
    const retryCount = Math.max(0, Number(prior.retryCount) || 0);
    let submittedAt = prior.submittedAt ? Date.parse(prior.submittedAt) : 0;
    const resuming = prior.globalIndex === item.globalIndex && prior.phase === "submitted";
    if (!resuming) {
      const startedAt = prior.globalIndex === item.globalIndex && prior.startedAt ? prior.startedAt : new Date().toISOString();
      await message({ type: "SET_IN_FLIGHT", inFlight: {
        ...item, phase: "preparing", retryCount,
        silentFailureCount: Math.max(0, Number(prior.silentFailureCount) || 0),
        startedAt, previousUrl: location.href
      } });
      // Wait first, then create and use a fresh conversation immediately.
      // Opening a new chat before a ten-minute rate-limit wait left DeepSeek's
      // transient route idle long enough to be re-rendered or invalidated,
      // which produced titled but empty conversations on the later click.
      if (!(await waitForSendSlot())) return null;
      // Reset only after the wait. Resetting before a six-minute wait allowed
      // late network responses from the previous conversation to leak into
      // the next round's source list.
      currentSources = [];
      networkSourceCount = 0;
      window.dispatchEvent(new CustomEvent("deepseek-monitor:reset"));
      const composer = await createNewChat();
      await ensureSmartSearch();
      const submission = await submitPrompt(composer, item.prompt);
      submittedAt = Date.now();
      await message({ type: "SET_IN_FLIGHT", inFlight: {
        ...item, phase: "submitted", retryCount, startedAt,
        submittedAt: new Date(submittedAt).toISOString(), previousUrl: location.href,
        conversationUrl: location.href,
        conversationId: submission?.currentConversationId || Core.conversationIdFromUrl(location.href)
      } });
    } else {
      const expectedConversationId = prior.conversationId || Core.conversationIdFromUrl(prior.conversationUrl || prior.previousUrl || "");
      const currentConversationId = Core.conversationIdFromUrl(location.href);
      if (expectedConversationId && currentConversationId !== expectedConversationId) {
        await logEvent("submitted_conversation_restore", "warning", {
          prompt: item.prompt, conversation_url: prior.conversationUrl || prior.previousUrl,
          current_url: location.href
        });
        location.assign(prior.conversationUrl || prior.previousUrl);
        return null;
      }
      if (!findPromptElement(item.prompt)) {
        await logEvent("submitted_conversation_hydrating", "info", {
          prompt: item.prompt, page_url: location.href,
          note: "页面尚未显示问题，继续按完整回答超时等待，不重复发送"
        });
      }
    }

    const candidate = await waitForAnswer(item.prompt, settings, submittedAt || Date.now(), retryCount);
    let domSources = [];
    let expected = 0;
    let rawSources = [];
    let sources = [];
    try {
      for (let sourcePass = 1; sourcePass <= 3; sourcePass += 1) {
        domSources = Core.dedupeSources(domSources.concat(await revealAndCollectSources(candidate)));
        expected = Math.max(expected, expectedSourceCount(candidate));
        rawSources = Core.dedupeSources(currentSources.concat(domSources));
        sources = Core.sanitizeSourceTitles(Core.selectCitationSources(rawSources, expected), candidate.text);
        if (!expected || sources.length >= expected) break;
        await logEvent("source_capture_retry", "warning", {
          prompt: item.prompt, source_pass: sourcePass,
          source_count: sources.length, expected_source_count: expected
        });
        await sleep(1200);
      }
    } catch (error) {
      expected = Math.max(expected, expectedSourceCount(candidate));
      rawSources = Core.dedupeSources(currentSources.concat(domSources).concat(collectDomSources(candidate)));
      sources = Core.sanitizeSourceTitles(Core.selectCitationSources(rawSources, expected), candidate.text);
      await logEvent("source_capture_degraded", "warning", {
        prompt: item.prompt, error: String(error && error.message || error),
        source_count: sources.length, expected_source_count: expected
      });
    }
    const finishedAt = new Date();
    const startedAt = job.inFlight?.startedAt || new Date(submittedAt || Date.now()).toISOString();
    const result = {
      schema_version: 1,
      result_id: crypto.randomUUID(),
      status: "success",
      skip_reason: "",
      collector_model: "deepseek",
      run_id: job.id,
      round: item.globalIndex + 1,
      question_index: item.questionIndex,
      question_round: item.questionRound,
      prompt: item.prompt,
      reply: candidate.text,
      web_body: candidate.text,
      sources,
      expected_source_count: expected,
      page_reported_source_count: expected,
      source_capture_complete: expected === 0 ? true : sources.length >= expected,
      source_count_basis: expected ? "citation_markers" : "no_page_count",
      page_url: location.href,
      page_title: document.title,
      detected_model: detectedModel(),
      started_at: startedAt,
      submitted_at: new Date(submittedAt || Date.now()).toISOString(),
      finished_at: finishedAt.toISOString(),
      duration_ms: Math.max(0, finishedAt.getTime() - Date.parse(startedAt)),
      capture: {
        answer_selector_hint: candidate.selectorHint || "",
        answer_completion_mode: candidate.completionMode || "stable",
        network_source_events: networkSourceCount,
        dom_source_count: domSources.length,
        raw_source_count: rawSources.length,
        filtered_source_count: sources.length
      }
    };
    await logEvent("capture_complete", "info", {
      prompt: item.prompt, reply_chars: candidate.text.length,
      source_count: sources.length, raw_source_count: rawSources.length,
      expected_source_count: expected, source_capture_complete: result.source_capture_complete,
      page_url: location.href, sources
    });
    return result;
  }

  async function runLoop() {
    if (runnerActive) return;
    runnerActive = true;
    try {
      const claim = await message({ type: "CLAIM_RUNNER" });
      if (!claim.claimed || !claim.job) return;
      while (true) {
        const context = await message({ type: "GET_JOB" });
        const job = context.job;
        const settings = context.settings || {};
        if (!job || job.state === "stopped" || job.state === "completed" || job.state === "error") break;
        if (job.state === "paused") { await sleep(1000); continue; }
        if (job.cursor >= job.schedule.length) {
          await message({ type: "COMPLETE_JOB" });
          break;
        }
        try {
          const result = await processItem(job, settings);
          if (!result) break;
          await message({ type: "STORE_RESULT", result });
          await sleep(1000);
        } catch (error) {
          const current = await message({ type: "GET_JOB" });
          const inFlight = current.job?.inFlight || {};
          const attempts = Math.max(0, Number(inFlight.retryCount) || 0);
          const maxRetries = Math.max(0, Math.min(10, Number(settings.maxRetries) || 3));
          if (error?.cooldownSeconds) {
            const silentFailureCount = Math.max(0, Number(inFlight.silentFailureCount) || 0) + 1;
            // Keep a bounded safety pause. Exponential 1h/2h/4h backoff left a
            // recoverable browser submission issue idle for most of the day.
            const cooldownSeconds = Math.max(10 * 60, Math.min(30 * 60, Number(error.cooldownSeconds) || 30 * 60));
            const reason = String(error && error.message || error);
            await message({ type: "SET_IN_FLIGHT", inFlight: {
              ...(job.schedule[job.cursor] || inFlight), phase: "retry_pending", retryCount: 0,
              silentFailureCount, lastFailure: reason, failedAt: new Date().toISOString()
            } });
            await logEvent("silent_submission_cooldown", "warning", {
              error: reason, silent_failure_count: silentFailureCount,
              cooldown_seconds: cooldownSeconds,
              resume_at: new Date(Date.now() + cooldownSeconds * 1000).toISOString()
            });
            await message({ type: "DEFER_JOB", error: reason, cooldownSeconds });
            break;
          }
          if (error?.pauseJob) {
            const reason = String(error && error.message || error);
            await logEvent("risk_control_paused", "error", { error: reason });
            await message({ type: "PAUSE_JOB", error: reason });
            break;
          }
          if (error?.retryable && attempts < maxRetries) {
            const nextAttempt = attempts + 1;
            const delay = Math.max(20, Number(settings.retryDelaySeconds) || 30);
            await logEvent("round_retry_scheduled", "warning", {
              error: String(error && error.message || error), retry_attempt: nextAttempt,
              max_retries: maxRetries, retry_seconds: delay
            });
            // A confirmed send is an idempotency boundary.  Any later failure
            // (DOM hydration, answer selector, source panel, page reload) may
            // retry capture, but must never submit the question again.
            const preserveSubmission = Boolean(
              inFlight.phase === "submitted" && error?.preserveSubmission !== false
            );
            await message({ type: "SET_IN_FLIGHT", inFlight: preserveSubmission ? {
              ...inFlight, phase: "submitted", retryCount: nextAttempt,
              lastFailure: String(error && error.message || error), failedAt: new Date().toISOString()
            } : {
              ...(job.schedule[job.cursor] || inFlight), phase: "retry_pending", retryCount: nextAttempt,
              lastFailure: String(error && error.message || error), failedAt: new Date().toISOString()
            } });
            await sleep(delay * 1000);
            if (error.reloadPage) {
              await logEvent("page_reload_for_retry", "warning", {
                error: String(error && error.message || error), retry_attempt: nextAttempt
              });
              if (preserveSubmission && inFlight.conversationUrl && location.href !== inFlight.conversationUrl) {
                location.assign(inFlight.conversationUrl);
              } else {
                location.reload();
              }
              return;
            }
            continue;
          }
          if (error?.retryable && attempts >= maxRetries) {
            const failedItem = job.schedule[job.cursor] || job.inFlight || {};
            const finishedAt = new Date();
            const startedAt = current.job?.inFlight?.startedAt || finishedAt.toISOString();
            const failure = {
              schema_version: 1, result_id: crypto.randomUUID(), status: "failed",
              skip_reason: String(error && error.message || error), collector_model: "deepseek",
              run_id: job.id, round: Number(failedItem.globalIndex ?? job.cursor) + 1,
              question_index: failedItem.questionIndex, question_round: failedItem.questionRound,
              prompt: failedItem.prompt || "", reply: "", web_body: "", sources: [],
              expected_source_count: 0, page_reported_source_count: 0,
              source_capture_complete: false, source_count_basis: "failed_round",
              page_url: location.href, page_title: document.title, detected_model: detectedModel(),
              started_at: startedAt, submitted_at: current.job?.inFlight?.submittedAt || startedAt,
              finished_at: finishedAt.toISOString(),
              duration_ms: Math.max(0, finishedAt.getTime() - Date.parse(startedAt)),
              capture: { failure_code: error.code || "DM_TRANSIENT", retry_count: attempts }
            };
            await logEvent("round_failed_continuing", "error", {
              error: failure.skip_reason, retry_count: attempts, next_round: failure.round + 1
            });
            await message({ type: "STORE_RESULT", result: failure });
            if (error.reloadPage) {
              await logEvent("page_reload_for_retry", "warning", {
                error: "本轮已记录失败；刷新页面后继续下一轮", retry_attempt: "下一轮"
              });
              await sleep(3000);
              location.reload();
              return;
            }
            await sleep(1000);
            continue;
          }
          await logEvent("round_error", "error", { error: String(error && error.message || error) });
          await message({ type: "JOB_ERROR", error: String(error && error.message || error) });
          break;
        }
      }
    } finally {
      runnerActive = false;
    }
  }

  chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
    if (request?.type === "DM_RUN") {
      runLoop();
      sendResponse({ ok: true });
    } else if (request?.type === "DM_PROBE") {
      sendResponse({ ok: true, url: location.href, hasComposer: Boolean(findComposer()), hasNewChat: canCreateNewChat() });
    }
    return false;
  });

  logEvent("extension_runtime_ready", "info", { content_version: "0.2.22", page_url: location.href });
  message({ type: "GET_JOB" }).then((context) => {
    if (context.job?.state === "running") runLoop();
  }).catch(() => {});
})();
