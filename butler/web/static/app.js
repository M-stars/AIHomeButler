(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  // ---- Token 管理 ----
  const TOKEN_KEY = "butler_token";
  function initToken() {
    const params = new URLSearchParams(location.search);
    const fromUrl = params.get("token");
    if (fromUrl) {
      localStorage.setItem(TOKEN_KEY, fromUrl);
      history.replaceState(null, "", location.pathname);
    }
    return localStorage.getItem(TOKEN_KEY) || "";
  }
  let token = initToken();

  function promptForToken(msg) {
    const val = window.prompt(msg || "请输入 AI Home Butler 访问令牌（启动控制台打印的 WEB_TOKEN）：", token);
    if (val && val.trim()) {
      token = val.trim();
      localStorage.setItem(TOKEN_KEY, token);
      connectSSE();
      refreshAll();
    }
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Butler-Token": token }, opts.headers || {});
    if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (resp.status === 401) {
      toast("访问令牌无效或缺失，请重新输入", "bad");
      promptForToken("令牌校验失败（401），请输入正确的访问令牌：");
      throw new Error("Unauthorized");
    }
    const ct = resp.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
      const detail = typeof data === "object" && data?.detail ? data.detail : String(data);
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    return data;
  }

  // ---- Toast ----
  function toast(msg, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("toastStack").appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  // ---- Theme ----
  const savedTheme = localStorage.getItem("butler_theme") || "dark";
  document.documentElement.setAttribute("data-theme", savedTheme);
  $("themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("butler_theme", next);
  };

  $("tokenResetBtn").onclick = () => promptForToken("更新访问令牌：");
  $("refreshBtn").onclick = () => {
    refreshAll();
    toast("已刷新全部状态", "ok");
  };

  // ---- Tab 切换 ----
  const TAB_TITLES = {
    dashboard: "设备总览与直控",
    chat: "AI 管家对话与安全流水线",
    routines: "例行任务与主动提醒",
    memory: "长期记忆与家庭台账",
    settings: "安全审计、多端配对与语音设置",
  };
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
      $("pageTitle").textContent = TAB_TITLES[tab] || "AI Home Butler";
    });
  });

  // ---- SSE 实时事件流 ----
  let sse = null;
  let sseRetryTimer = null;
  function connectSSE() {
    if (sse) {
      sse.close();
      sse = null;
    }
    if (!token) return;
    clearTimeout(sseRetryTimer);
    sse = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
    sse.addEventListener("connected", () => {
      $("sseDot").className = "dot ok";
      $("sseText").textContent = "SSE 实时通道在线";
    });
    sse.addEventListener("pipeline_step", (e) => {
      try {
        renderPipelineStep(JSON.parse(e.data));
      } catch (_) {}
    });
    sse.addEventListener("confirmation_pending", () => loadConfirmations());
    sse.addEventListener("confirmation_resolved", () => {
      loadConfirmations();
      loadOverview();
      loadAudit();
    });
    sse.addEventListener("device_updated", () => {
      loadOverview();
      loadAudit();
    });
    sse.addEventListener("notification", () => loadNotifications());
    sse.onerror = () => {
      $("sseDot").className = "dot warn";
      $("sseText").textContent = "SSE 重连中…";
      if (sse) sse.close();
      sseRetryTimer = setTimeout(connectSSE, 3500);
    };
  }

  // ---- 8 步安全流水线渲染 ----
  let lastTraceId = null;
  function resetPipeline(toolName) {
    $("pipeCurrentTool").textContent = toolName || "空闲";
    document.querySelectorAll("#pipelineSteps .pipe-step").forEach((el) => {
      el.className = "pipe-step";
      el.querySelector(".pipe-detail").textContent = "待命";
    });
  }
  function renderPipelineStep(ev) {
    if (ev.trace_id && ev.trace_id !== lastTraceId) {
      lastTraceId = ev.trace_id;
      resetPipeline(`${ev.tool} (${ev.actor || "agent"})`);
    }
    const row = document.querySelector(`#pipelineSteps .pipe-step[data-step="${ev.step}"]`);
    if (!row) return;
    row.className = `pipe-step ${ev.status || "ok"}`;
    row.querySelector(".pipe-detail").textContent = ev.detail || ev.status;
  }

  // ---- 设备图标与直控渲染 ----
  const DEVICE_ICONS = {
    light: "💡",
    fan: "🌀",
    switch: "🔌",
    sensor: "🌡️",
    cover: "🪟",
    climate: "❄️",
    humidifier: "💧",
    vacuum: "🤖",
  };

  let latestDevices = [];

  async function sendDirectCommand(deviceId, action, value) {
    try {
      const body = value !== undefined && value !== null ? { action, value } : { action };
      const res = await api(`/api/devices/${encodeURIComponent(deviceId)}/command`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (res.status === "ok") {
        toast(`已执行 ${action}（${res.verified ? "已回读验证" : res.stage}）`, "ok");
      } else if (res.status === "unknown") {
        toast(`状态未知：${res.message}`, "warn");
      }
      await loadOverview();
    } catch (err) {
      toast(`控制失败：${err.message}`, "bad");
      await loadOverview();
    }
  }

  function renderDevices(devices) {
    latestDevices = devices || [];
    const total = latestDevices.length;
    const online = latestDevices.filter((d) => d.available).length;
    const active = latestDevices.filter((d) => d.state && (d.state.on === true || d.state.status === "cleaning")).length;

    $("kpiTotal").textContent = total;
    $("kpiOnline").textContent = `在线 ${online} / 离线 ${total - online}`;
    $("kpiActive").textContent = active;

    // 同步例行任务设备下拉框
    const rtSel = $("rtDevice");
    const controllable = latestDevices.filter((d) => d.actions && d.actions.length > 0);
    const prevVal = rtSel.value;
    rtSel.innerHTML = controllable.length
      ? controllable.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} (${esc(d.area)})</option>`).join("")
      : `<option value="">暂无可控设备</option>`;
    if (prevVal && controllable.some((d) => d.id === prevVal)) rtSel.value = prevVal;

    // 按区域分组
    const byArea = {};
    for (const d of latestDevices) {
      const area = d.area || "未分区";
      (byArea[area] = byArea[area] || []).push(d);
    }
    const areaNames = Object.keys(byArea);
    if (!areaNames.length) {
      $("deviceAreas").innerHTML = `<div class="card small">暂无已纳管设备。</div>`;
      return;
    }

    $("deviceAreas").innerHTML = areaNames
      .map((area) => {
        const cards = byArea[area].map((d) => renderDeviceCard(d)).join("");
        return `<div class="area-section">
          <div class="area-title">📍 ${esc(area)} <span class="pill">${byArea[area].length}</span></div>
          <div class="device-grid">${cards}</div>
        </div>`;
      })
      .join("");

    // 绑定直控事件
    $("deviceAreas").querySelectorAll("[data-cmd-toggle]").forEach((el) => {
      el.addEventListener("change", () => {
        sendDirectCommand(el.dataset.cmdToggle, el.checked ? "turn_on" : "turn_off");
      });
    });
    $("deviceAreas").querySelectorAll("[data-cmd-range]").forEach((el) => {
      el.addEventListener("change", () => {
        sendDirectCommand(el.dataset.cmdRange, el.dataset.action, Number(el.value));
      });
    });
    $("deviceAreas").querySelectorAll("[data-cmd-btn]").forEach((el) => {
      el.addEventListener("click", () => {
        const val = el.dataset.value !== "" ? (isNaN(Number(el.dataset.value)) ? el.dataset.value : Number(el.dataset.value)) : undefined;
        sendDirectCommand(el.dataset.cmdBtn, el.dataset.action, val);
      });
    });
  }

  function renderDeviceCard(d) {
    const icon = DEVICE_ICONS[d.type] || "📦";
    const st = d.state || {};
    const isOn = st.on === true || st.status === "cleaning";
    const cls = ["device-card", isOn ? "is-on" : "", !d.available ? "is-offline" : ""].join(" ");
    const actions = d.actions || [];
    let bodyHtml = "";

    if (d.type === "sensor") {
      const parts = [];
      if (st.temperature !== undefined) parts.push(`${st.temperature}°C`);
      if (st.humidity !== undefined) parts.push(`${st.humidity}%`);
      if (st.value !== undefined) parts.push(`${st.value}${st.unit || ""}`);
      if (st.motion !== undefined) parts.push(st.motion ? "有人移动" : "无人");
      const reading = parts.length ? parts.join(" · ") : d.available ? "在线" : "离线";
      const freshTag = st.fresh === false ? `<span class="pill warn">数据陈旧</span>` : `<span class="pill ok">实时</span>`;
      bodyHtml = `<div class="control-row">
        <div class="device-reading">${esc(reading)}</div>
        ${d.available ? freshTag : `<span class="pill bad">离线</span>`}
      </div>`;
    } else {
      const controls = [];
      if (actions.includes("turn_on") && actions.includes("turn_off")) {
        controls.push(`<div class="control-row">
          <span class="small">${isOn ? "已开启" : "已关闭"}</span>
          <label class="switch">
            <input type="checkbox" data-cmd-toggle="${esc(d.id)}" ${isOn ? "checked" : ""} ${!d.available ? "disabled" : ""}>
            <span class="switch-slider"></span>
          </label>
        </div>`);
      }
      if (actions.includes("set_brightness")) {
        const b = st.brightness ?? 80;
        controls.push(`<div>
          <div class="control-row small"><span>亮度</span><span class="mono">${b}%</span></div>
          <input type="range" min="1" max="100" value="${b}" data-cmd-range="${esc(d.id)}" data-action="set_brightness" ${!d.available ? "disabled" : ""}>
        </div>`);
      }
      if (actions.includes("set_speed")) {
        const cur = st.speed || "low";
        controls.push(`<div class="control-row">
          <span class="small">风速</span>
          <div class="seg-group">
            ${["low", "medium", "high"]
              .map((sp) => `<button class="seg-btn ${cur === sp ? "active" : ""}" data-cmd-btn="${esc(d.id)}" data-action="set_speed" data-value="${sp}">${{ low: "低", medium: "中", high: "高" }[sp]}</button>`)
              .join("")}
          </div>
        </div>`);
      }
      if (actions.includes("set_temperature")) {
        const t = st.temperature ?? 25;
        controls.push(`<div class="control-row">
          <span class="small">目标温度</span>
          <div class="row" style="gap:4px">
            <button class="btn btn-ghost btn-sm" data-cmd-btn="${esc(d.id)}" data-action="set_temperature" data-value="${t - 1}">-</button>
            <span class="mono" style="min-width:44px;text-align:center">${t}°C</span>
            <button class="btn btn-ghost btn-sm" data-cmd-btn="${esc(d.id)}" data-action="set_temperature" data-value="${t + 1}">+</button>
          </div>
        </div>`);
      }
      if (actions.includes("set_position") || actions.includes("open")) {
        const pos = st.position ?? 0;
        controls.push(`<div class="control-row">
          <span class="small">窗帘位置 ${pos}%</span>
          <div class="seg-group">
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="open" data-value="">开</button>
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="stop" data-value="">停</button>
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="close" data-value="">关</button>
          </div>
        </div>`);
      }
      if (actions.includes("start")) {
        controls.push(`<div class="control-row">
          <span class="small">${esc(st.status || "待机")}</span>
          <div class="seg-group">
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="start" data-value="">清扫</button>
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="pause" data-value="">暂停</button>
            <button class="seg-btn" data-cmd-btn="${esc(d.id)}" data-action="return_to_base" data-value="">回充</button>
          </div>
        </div>`);
      }
      bodyHtml = `<div class="device-controls">${controls.join("") || `<span class="small">无可用控件</span>`}</div>`;
    }

    return `<div class="${cls}">
      <div class="device-top">
        <div class="device-icon">${icon}</div>
        <div class="device-meta">
          <div class="device-name">${esc(d.name)}</div>
          <div class="device-id">${esc(d.id)}</div>
        </div>
        <span class="pill ${d.available ? "ok" : "bad"}">${d.available ? "在线" : "离线"}</span>
      </div>
      ${bodyHtml}
    </div>`;
  }

  // ---- 数据加载 ----
  async function loadOverview() {
    try {
      const [ov, info] = await Promise.all([api("/api/overview"), api("/api/system/info")]);
      $("modelPill").textContent = `模型：${ov.model}`;
      $("confirmModePill").textContent = `确认制：${ov.confirmation_mode === "always" ? "严格开启" : "调试关闭"}`;
      $("confirmModePill").className = `pill ${ov.confirmation_mode === "always" ? "ok" : "warn"}`;
      $("sideBackend").textContent = ov.backend;
      $("kpiPaired").textContent = info.paired_devices_count ?? 0;

      const br = ov.ha_circuit_breaker;
      if (br) {
        $("sideBreaker").textContent = br.state.toUpperCase();
        $("sideBreaker").style.color = br.state === "closed" ? "var(--ok)" : br.state === "open" ? "var(--bad)" : "var(--warn)";
      } else {
        $("sideBreaker").textContent = ov.backend === "homeassistant" ? "CLOSED" : "直连/模拟";
      }

      if (ov.token_budget) {
        const used = ov.token_budget.used_today ?? 0;
        const limit = ov.token_budget.daily_limit ?? 0;
        $("kpiTokens").textContent = used.toLocaleString();
        $("tokenPill").textContent = limit > 0 ? `今日 Token：${used.toLocaleString()} / ${limit.toLocaleString()}` : `今日 Token：${used.toLocaleString()}`;
        const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
        $("kpiTokenBar").style.width = `${pct}%`;
      }
      renderDevices(ov.devices);
    } catch (_) {}
  }

  async function loadConfirmations() {
    try {
      const data = await api("/api/confirmations");
      const list = data.confirmations || [];
      const badge = $("confirmBadge");
      badge.textContent = list.length;
      badge.style.display = list.length ? "inline-block" : "none";
      const banner = $("confirmBanner");
      banner.classList.toggle("visible", list.length > 0);
      $("confirmList").innerHTML = list
        .map(
          (c) => `<div class="confirm-item">
            <div>
              <div style="font-weight:700">${esc(c.summary)}</div>
              <div class="small mono">${esc(JSON.stringify(c.arguments))}</div>
            </div>
            <div class="row">
              <button class="btn btn-ok btn-sm" data-confirm-id="${esc(c.id)}" data-approve="1">✓ 批准执行</button>
              <button class="btn btn-danger btn-sm" data-confirm-id="${esc(c.id)}" data-approve="0">✕ 拒绝</button>
            </div>
          </div>`
        )
        .join("");
      $("confirmList").querySelectorAll("[data-confirm-id]").forEach((btn) => {
        btn.onclick = async () => {
          const approved = btn.dataset.approve === "1";
          await api(`/api/confirmations/${encodeURIComponent(btn.dataset.confirmId)}`, {
            method: "POST",
            body: JSON.stringify({ approved }),
          });
          toast(approved ? "已批准操作" : "已拒绝操作", approved ? "ok" : "warn");
          loadConfirmations();
        };
      });
    } catch (_) {}
  }

  // ---- 对话与任务 ----
  function appendBubble(cls, text) {
    const div = document.createElement("div");
    div.className = `bubble ${cls}`;
    div.textContent = text;
    $("chatLog").appendChild(div);
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
  }

  async function sendChat() {
    const input = $("chatInput");
    const raw = input.value.trim();
    if (!raw) return;
    input.value = "";
    const isTask = $("taskModeToggle").checked || raw.startsWith("/task ");
    const text = raw.startsWith("/task ") ? raw.slice(6).trim() : raw;
    appendBubble("user", isTask ? `[多步任务] ${text}` : text);
    $("sendBtn").disabled = true;

    try {
      if (isTask) {
        const res = await api("/api/task", { method: "POST", body: JSON.stringify({ text }) });
        (res.steps || []).forEach((s) =>
          appendBubble("tool", `步骤 ${s.tool} → ${s.status}：${s.message || ""}`)
        );
        appendBubble("butler", res.summary || `任务状态：${res.status}`);
        loadTasks();
      } else {
        const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ text }) });
        (res.tool_events || []).forEach((e) =>
          appendBubble("tool", `🔧 ${e.tool} → ${e.status}：${e.message}`)
        );
        appendBubble("butler", res.reply);
        if ($("speakReplyToggle").checked && res.reply) {
          playTTS(res.reply);
        }
      }
      loadOverview();
      loadAudit();
      loadMemories();
    } catch (err) {
      appendBubble("butler", `⚠️ 请求失败：${err.message}`);
    } finally {
      $("sendBtn").disabled = false;
    }
  }

  async function playTTS(text) {
    try {
      const resp = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Butler-Token": token },
        body: JSON.stringify({ text }),
      });
      if (!resp.ok) return;
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      audio.play().catch(() => {});
    } catch (_) {}
  }

  $("sendBtn").onclick = sendChat;
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat();
  });
  document.querySelectorAll(".chip[data-prompt]").forEach((chip) => {
    chip.onclick = () => {
      $("chatInput").value = chip.dataset.prompt;
      sendChat();
    };
  });

  // ---- 例行任务、规则、通知、任务日志 ----
  async function loadRoutines() {
    try {
      const data = await api("/api/routines");
      const list = data.routines || [];
      $("routineList").innerHTML = list.length
        ? list
            .map((r) => {
              const sch = r.schedule || {};
              const acts = (r.actions || []).map((a) => `${a.device_id}:${a.action}`).join("、");
              return `<div class="list-item">
                <div>
                  <div style="font-weight:600">${esc(r.name)} <span class="pill ${r.enabled ? "ok" : ""}">${r.enabled ? "启用" : "停用"}</span></div>
                  <div class="small mono">${esc(sch.time || "")} · ${esc((sch.days || []).join(","))} · ${esc(acts)}</div>
                </div>
                <div class="row">
                  <button class="btn btn-ghost btn-sm" data-rt-toggle="${r.id}">${r.enabled ? "停用" : "启用"}</button>
                  <button class="btn btn-danger btn-sm" data-rt-del="${r.id}">删除</button>
                </div>
              </div>`;
            })
            .join("")
        : "暂无例行任务。";
      $("routineList").querySelectorAll("[data-rt-toggle]").forEach((b) => {
        b.onclick = async () => {
          await api(`/api/routines/${b.dataset.rtToggle}/toggle`, { method: "POST" });
          loadRoutines();
        };
      });
      $("routineList").querySelectorAll("[data-rt-del]").forEach((b) => {
        b.onclick = async () => {
          await api(`/api/routines/${b.dataset.rtDel}`, { method: "DELETE" });
          toast("已删除例行任务", "ok");
          loadRoutines();
        };
      });
    } catch (_) {}
  }

  $("rtCreateBtn").onclick = async () => {
    const name = $("rtName").value.trim();
    const time = $("rtTime").value;
    const days = $("rtDays").value;
    const device_id = $("rtDevice").value;
    const action = $("rtAction").value;
    if (!name || !device_id) {
      toast("请填写任务名称并选择可控设备", "warn");
      return;
    }
    try {
      await api("/api/routines", {
        method: "POST",
        body: JSON.stringify({ name, time, days, actions: [{ device_id, action }] }),
      });
      $("rtName").value = "";
      toast("例行任务创建成功", "ok");
      loadRoutines();
      loadAudit();
    } catch (err) {
      toast(`创建失败：${err.message}`, "bad");
    }
  };

  async function loadNotifications() {
    try {
      const [nData, rData] = await Promise.all([api("/api/notifications"), api("/api/rules")]);
      const notifs = nData.notifications || [];
      $("notifList").innerHTML = notifs.length
        ? notifs
            .slice(0, 12)
            .map((n) => `<div class="notif-card"><b>${esc(n.rule_name || "提醒")}</b> · <span class="small">${esc(n.created_at)}</span><div>${esc(n.message)}</div></div>`)
            .join("")
        : "暂无主动提醒。";
      const rules = rData.rules || [];
      $("ruleList").innerHTML = rules.length
        ? rules
            .map((r) => `<div class="list-item"><div><b>${esc(r.name)}</b> <span class="pill">${esc(r.kind)}</span></div><span class="pill ${r.enabled ? "ok" : ""}">${r.enabled ? "运行中" : "暂停"}</span></div>`)
            .join("")
        : "暂无自定义规则。";
    } catch (_) {}
  }

  async function loadTasks() {
    try {
      const data = await api("/api/tasks");
      const list = data.tasks || [];
      $("taskLogList").innerHTML = list.length
        ? list
            .slice(0, 10)
            .map((t) => `<div class="list-item"><div><b>${esc(t.user_text)}</b><div class="small">${esc(t.created_at)}</div></div><span class="pill ${t.outcome === "completed" ? "ok" : "warn"}">${esc(t.outcome)}</span></div>`)
            .join("")
        : "暂无多步骤任务记录。";
    } catch (_) {}
  }

  // ---- 记忆与家庭台账 ----
  async function loadMemories() {
    try {
      const data = await api("/api/memories");
      const list = data.memories || [];
      $("memList").innerHTML = list.length
        ? list
            .map(
              (m) => `<div class="list-item">
                <div>
                  <span class="pill accent">${m.kind === "preference" ? "偏好" : "事实"}</span>
                  <span>${esc(m.content)}</span>
                  <div class="small">${esc(m.updated_at)} · 来源：${esc(m.source)}${m.member_id ? ` · 成员 ${esc(m.member_id)}` : " · 共享"}</div>
                </div>
                <button class="btn btn-danger btn-sm" data-mem-del="${m.id}">删除</button>
              </div>`
            )
            .join("")
        : "暂无已保存记忆。";
      $("memList").querySelectorAll("[data-mem-del]").forEach((b) => {
        b.onclick = async () => {
          await api(`/api/memories/${b.dataset.memDel}`, { method: "DELETE" });
          toast("已物理删除记忆", "ok");
          loadMemories();
          loadAudit();
        };
      });
    } catch (_) {}
  }

  $("memAddBtn").onclick = async () => {
    const content = $("memContent").value.trim();
    if (!content) return;
    try {
      await api("/api/memories", {
        method: "POST",
        body: JSON.stringify({ kind: $("memKind").value, content }),
      });
      $("memContent").value = "";
      toast("记忆已保存", "ok");
      loadMemories();
      loadAudit();
    } catch (err) {
      toast(err.message, "bad");
    }
  };

  async function loadHousehold() {
    try {
      const p = await api("/api/pantry");
      $("pantryList").innerHTML = p.items?.length
        ? p.items
            .map((i) => `<div class="list-item"><span>${esc(i.name)} (${esc(i.quantity || "1")})</span><span class="pill ${i.expired ? "bad" : "ok"}">${i.days_left != null ? `剩 ${i.days_left} 天` : "无保质期"}</span></div>`)
            .join("")
        : "暂无食材记录。";
    } catch (_) {
      $("pantryList").textContent = "家庭台账未启用。";
    }
    try {
      const w = await api("/api/wardrobe");
      $("wardrobeList").innerHTML = w.garments?.length
        ? w.garments
            .map((g) => `<div class="list-item"><span>${esc(g.name)} · ${esc(g.category)}</span><span class="pill">${esc(g.status)}</span></div>`)
            .join("")
        : "暂无衣物记录。";
    } catch (_) {
      $("wardrobeList").textContent = "衣柜台账未启用。";
    }
  }

  // ---- 多端配对（App / 小程序） ----
  let pairCountdown = null;
  $("pairStartBtn").onclick = async () => {
    try {
      const res = await api("/api/pair/start", { method: "POST" });
      $("pairCodeBox").classList.add("visible");
      $("pairCodeDigits").textContent = res.code;
      let rem = res.expires_in_seconds || 600;
      clearInterval(pairCountdown);
      const tick = () => {
        if (rem <= 0) {
          clearInterval(pairCountdown);
          $("pairCodeExpiry").textContent = "配对码已过期，请重新生成";
          return;
        }
        const m = Math.floor(rem / 60);
        const s = rem % 60;
        $("pairCodeExpiry").textContent = `有效期剩余 ${m} 分 ${s} 秒（最多 ${res.max_attempts} 次尝试）`;
        rem -= 1;
      };
      tick();
      pairCountdown = setInterval(tick, 1000);
      toast("已生成 6 位配对码", "ok");
      loadAudit();
    } catch (err) {
      toast(`生成配对码失败：${err.message}`, "bad");
    }
  };

  async function loadPairings() {
    try {
      const data = await api("/api/pair/devices");
      const list = data.devices || [];
      $("pairedList").innerHTML = list.length
        ? list
            .map(
              (d) => `<div class="list-item">
                <div>
                  <b>${esc(d.device_name)}</b> <span class="pill accent">${esc(d.platform)}</span>
                  <div class="small mono">${esc(d.device_id)} · 最近活跃：${esc(d.last_seen_at || d.created_at)}</div>
                </div>
                <button class="btn btn-danger btn-sm" data-pair-del="${esc(d.device_id)}">踢除</button>
              </div>`
            )
            .join("")
        : "暂无已配对客户端（可通过上方配对码接入 App 或小程序）。";
      $("pairedList").querySelectorAll("[data-pair-del]").forEach((b) => {
        b.onclick = async () => {
          await api(`/api/pair/devices/${encodeURIComponent(b.dataset.pairDel)}`, { method: "DELETE" });
          toast("已踢除客户端，令牌即时失效", "warn");
          loadPairings();
          loadOverview();
          loadAudit();
        };
      });
    } catch (_) {}
  }

  // ---- 人设与模型档案 ----
  async function loadSettings() {
    try {
      const [pData, mData] = await Promise.all([api("/api/settings/persona"), api("/api/settings/models")]);
      $("personaInput").value = pData.persona || "";
      $("personaPresets").innerHTML = (pData.presets || [])
        .map((p) => `<button class="chip" data-preset-prompt="${esc(p.prompt)}" title="${esc(p.description)}">${esc(p.name)}</button>`)
        .join("");
      $("personaPresets").querySelectorAll("[data-preset-prompt]").forEach((b) => {
        b.onclick = () => {
          $("personaInput").value = b.dataset.presetPrompt;
        };
      });
      $("modelProfiles").innerHTML = (mData.profiles || [])
        .map((m) => `<div class="list-item"><div><b>${esc(m.name)}</b> · <span class="mono">${esc(m.model)}</span><div class="small mono">${esc(m.base_url)}</div></div><span class="pill mono">${esc(m.api_key_masked)}</span></div>`)
        .join("");
    } catch (_) {}
  }

  $("personaSaveBtn").onclick = async () => {
    try {
      await api("/api/settings/persona", {
        method: "PUT",
        body: JSON.stringify({ persona: $("personaInput").value }),
      });
      toast("管家 AI 人设已保存并热生效", "ok");
      loadAudit();
    } catch (err) {
      toast(err.message, "bad");
    }
  };

  // ---- 语音与声纹 ----
  async function loadVoiceAndSpeakers() {
    try {
      const [st, vData] = await Promise.all([api("/api/voice/status"), api("/api/voices")]);
      $("voiceStatus").textContent = `TTS 后端：${st.backend || "sapi"} · 活跃音色：${vData.active_voice_id || "默认"}`;
      const voices = vData.voices || [];
      $("voiceList").innerHTML = voices.length
        ? voices.map((v) => `<div class="list-item"><span>${esc(v.display_name)}</span><span class="pill">${v.is_default ? "默认" : "可用"}</span></div>`).join("")
        : "当前使用系统默认离线语音。";
    } catch (_) {
      $("voiceStatus").textContent = "语音合成模块未装配（不影响核心控制功能）。";
    }
    try {
      const sData = await api("/api/speakers");
      $("speakerStatus").textContent = `声纹识别：${sData.enabled ? "已开启" : "未开启"} · 服务健康：${sData.service_healthy ? "正常" : "离线"}`;
      const members = sData.members || [];
      $("speakerList").innerHTML = members.length
        ? members.map((m) => `<div class="list-item"><span>${esc(m.display_name)}</span><span class="pill mono">${esc(m.member_id)}</span></div>`).join("")
        : "暂未注册家庭成员声纹。";
    } catch (_) {
      $("speakerStatus").textContent = "声纹识别模块未装配。";
    }
  }

  $("voiceCacheClearBtn").onclick = async () => {
    try {
      const res = await api("/api/voice/cache/clear", { method: "POST" });
      toast(`已清空语音缓存（${res.cleared ?? 0} 项）`, "ok");
    } catch (err) {
      toast(err.message, "warn");
    }
  };

  // ---- 审计日志 ----
  async function loadAudit() {
    try {
      const data = await api("/api/audit?limit=100");
      const rows = data.audit || [];
      $("auditBody").innerHTML = rows.length
        ? rows
            .map((a) => {
              const stCls = a.status === "ok" ? "ok" : a.status === "declined" ? "warn" : "bad";
              return `<tr>
                <td class="mono small">${esc(a.created_at)}</td>
                <td><span class="pill mono">${esc(a.actor)}</span></td>
                <td class="mono">${esc(a.action)}</td>
                <td><span class="pill ${stCls}">${esc(a.status)}</span></td>
                <td class="small">${esc(a.detail)}</td>
              </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="small">暂无审计记录</td></tr>`;
    } catch (_) {}
  }

  async function refreshAll() {
    if (!token) {
      promptForToken("首次打开控制台，请输入访问令牌：");
      return;
    }
    await Promise.all([
      loadOverview(),
      loadConfirmations(),
      loadRoutines(),
      loadNotifications(),
      loadTasks(),
      loadMemories(),
      loadHousehold(),
      loadPairings(),
      loadSettings(),
      loadVoiceAndSpeakers(),
      loadAudit(),
    ]);
  }

  // 启动
  connectSSE();
  refreshAll();
  setInterval(() => {
    if (token) {
      loadOverview();
      loadConfirmations();
    }
  }, 5000);
})();
