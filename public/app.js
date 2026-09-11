"use strict";

const state = {
  range: "all",
  sessionId: null,
  autoRefresh: true,
  timer: null,
};

const fmtInt = (v) => (Number(v) || 0).toLocaleString("en-US");
const fmtCredit = (v) => (Number(v) || 0).toFixed(3);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

function fmtTime(ms) {
  if (!ms) return "-";
  const d = new Date(Number(ms));
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 1600);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ------------------------------------------------------------------ cards */

function renderCards(totals) {
  const cards = [
    { k: "合计 tokens", v: fmtInt(totals.total), s: `${fmtInt(totals.requests)} 次请求` },
    { k: "输入", v: fmtInt(totals.input), s: `缓存命中 ${fmtInt(totals.cached)}` },
    {
      k: "缓存命中率",
      v: `${totals.cache_hit_rate}%`,
      s: `未命中 ${fmtInt(totals.cache_miss)}`,
    },
    {
      k: "输出",
      v: fmtInt(totals.output),
      s: `推理 ${fmtInt(totals.reasoning)}`,
    },
    { k: "计费(credit)", v: fmtCredit(totals.credit), s: "CodeBuddy 原生计数" },
    { k: "会话数", v: fmtInt(totals.sessions), s: "所选时间范围内" },
  ];
  document.getElementById("cards").innerHTML = cards
    .map(
      (c) =>
        `<div class="card"><div class="k">${esc(c.k)}</div><div class="v">${esc(
          c.v
        )}</div><div class="s">${esc(c.s || "")}</div></div>`
    )
    .join("");
}

/* ------------------------------------------------------------------ trend */

function renderTrend(byDay) {
  const el = document.getElementById("trend");
  if (!byDay.length) {
    el.innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }
  const max = Math.max(...byDay.map((d) => d.total), 1);
  el.innerHTML = `<div class="bars">${byDay
    .map((d) => {
      const h = Math.max(2, Math.round((d.total / max) * 140));
      return `<div class="col" title="${esc(d.day)}: ${fmtInt(
        d.total
      )} tokens, 缓存命中 ${d.cache_hit_rate}%">
        <div class="bar" style="height:${h}px"></div>
        <div class="lbl">${esc(d.day.slice(5))}</div>
      </div>`;
    })
    .join("")}</div>`;
}

/* ----------------------------------------------------------------- tables */

function rowsTable(rows, nameLabel) {
  if (!rows.length) return '<div class="loading">暂无数据</div>';
  return `<table><thead><tr>
      <th>${esc(nameLabel)}</th><th>请求</th><th>输入</th><th>命中率</th>
      <th>输出</th><th>推理</th><th>合计</th><th>计费</th>
    </tr></thead><tbody>${rows
      .map(
        (r) => `<tr>
        <td>${esc(r.name)}</td>
        <td>${fmtInt(r.requests)}</td>
        <td>${fmtInt(r.input)}</td>
        <td>${r.cache_hit_rate}%</td>
        <td>${fmtInt(r.output)}</td>
        <td>${fmtInt(r.reasoning)}</td>
        <td>${fmtInt(r.total)}</td>
        <td>${fmtCredit(r.credit)}</td>
      </tr>`
      )
      .join("")}</tbody></table>`;
}

function renderSessions(sessions) {
  const el = document.getElementById("sessions");
  if (!sessions.length) {
    el.innerHTML = '<div class="loading">暂无会话</div>';
    return;
  }
  el.innerHTML = `<div class="wrap"><table><thead><tr>
      <th>会话</th><th>标题</th><th>目录</th><th>轮次</th><th>请求</th>
      <th>输入</th><th>命中率</th><th>输出</th><th>合计</th><th>计费</th><th>最后活动</th>
    </tr></thead><tbody>${sessions
      .map((s) => {
        const sel = s.session_id === state.sessionId ? " sel" : "";
        return `<tr class="clickable${sel}" data-id="${esc(s.session_id)}">
        <td class="mono">${esc(s.session_id.slice(0, 12))}</td>
        <td>${esc(s.title || "-")}</td>
        <td class="mono muted">${esc(s.cwd || s.project || "-")}</td>
        <td>${fmtInt(s.turns)}</td>
        <td>${fmtInt(s.requests)}</td>
        <td>${fmtInt(s.input)}</td>
        <td>${s.cache_hit_rate}%</td>
        <td>${fmtInt(s.output)}</td>
        <td>${fmtInt(s.total)}</td>
        <td>${fmtCredit(s.credit)}</td>
        <td>${esc(fmtTime(s.last_ts))}</td>
      </tr>`;
      })
      .join("")}</tbody></table></div>`;

  el.querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => selectSession(tr.dataset.id))
  );
}

/* --------------------------------------------------------------- turns */

async function selectSession(id) {
  state.sessionId = id;
  document.querySelectorAll("#sessions tr.clickable").forEach((tr) =>
    tr.classList.toggle("sel", tr.dataset.id === id)
  );
  const panel = document.getElementById("turns-panel");
  const body = document.getElementById("turns");
  panel.style.display = "";
  body.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await fetchJSON(`/api/session?id=${encodeURIComponent(id)}&refresh=1`);
    renderTurns(data.session || {}, data.turns || []);
  } catch (err) {
    body.innerHTML = `<div class="err">加载失败：${esc(err.message)}</div>`;
  }
}

function renderTurns(session, turns) {
  const head = session.title || session.session_id || "";
  document.getElementById("turns-title").textContent = `轮次明细 · ${head}`;
  const body = document.getElementById("turns");
  if (!turns.length) {
    body.innerHTML = '<div class="loading">该会话暂无请求</div>';
    return;
  }
  body.innerHTML = `<div class="wrap"><table><thead><tr>
      <th>轮次</th><th>时间</th><th>模型</th><th>请求</th><th>输入</th>
      <th>命中率</th><th>输出</th><th>推理</th><th>合计</th><th>计费</th>
    </tr></thead><tbody>${turns
      .map(
        (t) => `<tr>
        <td>#${t.turn}</td>
        <td>${esc(fmtTime(t.last_ts))}</td>
        <td class="mono">${esc((t.models || []).join(", "))}</td>
        <td>${fmtInt(t.requests)}</td>
        <td>${fmtInt(t.input)}</td>
        <td>${t.cache_hit_rate}%</td>
        <td>${fmtInt(t.output)}</td>
        <td>${fmtInt(t.reasoning)}</td>
        <td>${fmtInt(t.total)}</td>
        <td>${fmtCredit(t.credit)}</td>
      </tr>`
      )
      .join("")}</tbody></table></div>`;
}

/* -------------------------------------------------------------- archive */

function renderArchive(records) {
  const el = document.getElementById("archive");
  if (!records.length) {
    el.innerHTML =
      '<div class="loading">暂无归档记录（hooks 生效后每轮结束写入）</div>';
    return;
  }
  el.innerHTML = `<div class="wrap"><table><thead><tr>
      <th>时间</th><th>会话</th><th>本轮合计</th><th>命中率</th><th>归档位置</th>
    </tr></thead><tbody>${records
      .map((r) => {
        const turn = r.turn || {};
        return `<tr>
        <td>${esc((r.ts || "").replace("T", " ").slice(5, 16))}</td>
        <td class="mono">${esc(String(r.session_id || "").slice(0, 12))}</td>
        <td>${fmtInt(turn.total)}</td>
        <td>${Number(r.turn_cache_hit_rate || 0).toFixed(0)}%</td>
        <td class="muted" style="text-align:left">${esc(r.archive || "")}</td>
      </tr>`;
      })
      .join("")}</tbody></table></div>`;
}

/* ------------------------------------------------------------------ load */

async function loadAll(force) {
  const q = `range=${state.range}${force ? "&refresh=1" : ""}`;
  try {
    const [summary, sessions, archive] = await Promise.all([
      fetchJSON(`/api/summary?${q}`),
      fetchJSON(`/api/sessions?${q}`),
      fetchJSON(`/api/archive?limit=40`),
    ]);
    renderCards(summary.totals);
    renderTrend(summary.by_day || []);
    document.getElementById("by-model").innerHTML = rowsTable(
      summary.by_model || [],
      "模型"
    );
    document.getElementById("by-project").innerHTML = rowsTable(
      summary.by_project || [],
      "项目"
    );
    renderSessions(sessions.sessions || []);
    renderArchive(archive.records || []);
    document.getElementById("generated").textContent = summary.generated_at || "";
    if (state.sessionId) selectSession(state.sessionId);
  } catch (err) {
    toast(`刷新失败：${err.message}`);
  }
}

function setAutoRefresh(on) {
  state.autoRefresh = on;
  document.getElementById("auto").classList.toggle("active", on);
  if (state.timer) clearInterval(state.timer);
  if (on) state.timer = setInterval(() => loadAll(false), 10000);
}

function init() {
  document.querySelectorAll("[data-range]").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.range = btn.dataset.range;
      document
        .querySelectorAll("[data-range]")
        .forEach((b) => b.classList.toggle("active", b === btn));
      loadAll(true);
    })
  );
  document.getElementById("refresh").addEventListener("click", () => {
    loadAll(true);
    toast("已刷新");
  });
  document.getElementById("auto").addEventListener("click", () =>
    setAutoRefresh(!state.autoRefresh)
  );
  const saved = localStorage.getItem("cb-theme");
  if (saved === "dark") document.body.classList.add("dark");
  document.getElementById("theme").addEventListener("click", () => {
    const dark = document.body.classList.toggle("dark");
    localStorage.setItem("cb-theme", dark ? "dark" : "light");
  });
  setAutoRefresh(true);
  loadAll(true);
}

document.addEventListener("DOMContentLoaded", init);
