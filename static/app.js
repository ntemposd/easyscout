// static/app.js
"use strict";

const sb = window.sb;

(function () {
  // ---------- Enhanced logging for debugging ----------
  const DEBUG = false; // Set to false to disable console logs
  const log = {
    info: (tag, msg, data = null) => {
      if (DEBUG) console.log(`%c[${tag}]%c ${msg}`, "color: #2563eb; font-weight: bold", "color: inherit", data || "");
    },
    warn: (tag, msg, data = null) => {
      if (DEBUG) console.warn(`%c[${tag}]%c ${msg}`, "color: #f59e0b; font-weight: bold", "color: inherit", data || "");
    },
    error: (tag, msg, data = null) => {
      if (DEBUG) console.error(`%c[${tag}]%c ${msg}`, "color: #ef4444; font-weight: bold", "color: inherit", data || "");
    },
    success: (tag, msg, data = null) => {
      if (DEBUG) console.log(`%c[${tag}]%c ${msg} ✅`, "color: #10b981; font-weight: bold", "color: inherit", data || "");
    }
  };
  
  // Expose global logger
  window.__debug = log;

  // ---------- tiny helpers ----------
  const $ = (id) => document.getElementById(id);

  const setText = (id, val = "") => {
    const el = $(id);
    if (el) {
      el.textContent = String(val ?? "");
      // Show/hide badge based on content
      if (id === "badge") {
        if (val) {
          el.classList.remove("hidden");
        } else {
          el.classList.add("hidden");
        }
      }
    }
  };
    // ---------- Reports sidebar (library) ----------
    let reportsState = {
      items: [],
      total: 0,
      loading: false,
      hasMore: true,
    };

    async function loadReports(q = "", reset = true) {
      const listEl = $("reports_list");
      const countEl = $("reports_count");
      if (!listEl) return;

      // Reset state on new search
      if (reset) {
        reportsState.items = [];
        reportsState.total = 0;
        reportsState.hasMore = true;
        listEl.innerHTML = '<div class="text-sm text-zinc-500">Loading…</div>';
        if (countEl) countEl.textContent = "0";
      }

      // Prevent duplicate simultaneous requests
      if (reportsState.loading || !reportsState.hasMore) return;
      reportsState.loading = true;

      try {
        const offset = reportsState.items.length;
        const limit = 50;

        const token = await (window.getAccessToken ? window.getAccessToken() : null);
        const res = await fetch(
          `/api/reports?q=${encodeURIComponent(q || "")}&limit=${limit}&offset=${offset}`,
          {
            headers: {
              "Content-Type": "application/json",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
          }
        );

        if (res.status === 401) {
          listEl.innerHTML = '<div class="text-sm text-red-600">Please log in to view your reports.</div>';
          if (countEl) countEl.textContent = "0";
          reportsState.loading = false;
          return;
        }

        const data = await res.json().catch(() => ({}));
        const newItems = Array.isArray(data.items) ? data.items : [];
        const total = Number.isFinite(data.total) ? data.total : 0;

        reportsState.items.push(...newItems);
        reportsState.total = total;
        reportsState.hasMore = newItems.length === limit && reportsState.items.length < total;
        reportsState.loading = false;

        if (countEl) countEl.textContent = `${total}`;

        if (!reportsState.items.length) {
          listEl.innerHTML = '<div class="text-sm text-zinc-500">No reports yet.</div>';
          return;
        }

        // Render all items
        listEl.innerHTML = reportsState.items
          .map((item) => {
            const player = escapeHtml(item.player_name || "Unknown");
            const position = escapeHtml(item.position || "Unknown");
            return `
              <button data-report-id="${item.id}" class="w-full text-left p-3 rounded-lg border border-zinc-200 hover:border-[#6FD06B] hover:bg-[#F7FBF8] transition flex flex-col gap-1">
                <span class="text-sm font-semibold text-zinc-900">${player}</span>
                <span class="text-xs text-zinc-500">${position}</span>
              </button>
            `;
          })
          .join("") + 
          (reportsState.hasMore ? '<div id="reports_loading" class="text-sm text-zinc-500 text-center py-2">Scroll for more…</div>' : '');

        // Setup scroll listener only once
        const container = listEl.parentElement;
        if (container && !container._scrollSetup && reportsState.hasMore) {
          container._scrollSetup = true;
          container.addEventListener("scroll", () => {
            const { scrollTop, scrollHeight, clientHeight } = container;
            if (!reportsState.loading && reportsState.hasMore && scrollHeight - scrollTop - clientHeight < 300) {
              loadReports(q, false);
            }
          });
        }
      } catch (err) {
        console.error("loadReports failed", err);
        reportsState.loading = false;
        if (reset) {
          listEl.innerHTML = '<div class="text-sm text-red-600">Failed to load reports.</div>';
        }
      }
    }

    async function openReportById(reportId) {
      if (!reportId) return;
      try {
        const token = await (window.getAccessToken ? window.getAccessToken() : null);
        const res = await fetch(`/api/reports/${reportId}`, {
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Failed to load report (${res.status})`);

        // Mark as cached since it's from library
        data.cached = true;

        // Store current report data for modal buttons
        window._currentReportData = {
          player: data.player || data.player_name || "Unknown",
          team: data.team || "",
          league: data.league || ""
        };
        window._currentCreditsBalance = data.credits_remaining;

        $("out_html").innerHTML = renderReport(data);
        window.enableTableDragScroll?.();
        setText("badge");
        setText("status", "");
        
        // Single scroll handler - scroll to "Player report" section
        const headers = document.querySelectorAll('h2');
        const reportHeader = Array.from(headers).find(h => h.textContent && h.textContent.includes('Player report'));
        if (reportHeader) {
          reportHeader.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
          window.scrollTo(0, 0);
        }

        // Track library report load
        try {
          window.trackClientEvent?.("library_report_loaded", {
            player_name: data.player || data.player_name || "Unknown",
            report_id: reportId
          });
        } catch (err) {}

        // Store regenerate target
        window._regenerateReportId = data.report_id || data.id || reportId;
      } catch (err) {
        console.error("openReportById failed", err);
        setText("err", err?.message || String(err));
      }
    }


  const on = (id, event, handler) => {
    const el = $(id);
    if (!el) return false;
    el.addEventListener(event, handler);
    return true;
  };

  // If we don't have the core app elements, do nothing (safe to load on every page)
  function isAppPage() {
    // `#example` may be omitted (was commented out). Only require the elements
    // we actually need to run: the Run button, player input, and output container.
    return !!($("run") && $("player") && $("out_html"));
  }

  // ---------- input helpers ----------
  function escapeHtml(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function cleanValue(v, fallback = "Unknown") {
    const s = String(v ?? "").trim();
    if (!s) return fallback;
    if (s.toLowerCase() === "unknown") return fallback;
    return s.replace(/\s*\(\s*\)\s*$/, "").trim() || fallback;
  }

  // ---------- Team / League helpers ----------
  function splitTeamLeague(v) {
    const s = String(v || "").trim();
    if (!s || s.toLowerCase() === "unknown") return { team: "Unknown", league: "Unknown" };

    // "Milwaukee Bucks (NBA)"
    const paren = s.match(/^(.+?)\s*\((.+?)\)\s*$/);
    if (paren) return { team: paren[1].trim() || "Unknown", league: paren[2].trim() || "Unknown" };

    const separators = [" / ", " — ", " – ", " - ", "|", "•", "·"];
    for (const sep of separators) {
      if (s.includes(sep)) {
        const parts = s.split(sep).map((x) => x.trim()).filter(Boolean);
        if (parts.length >= 2) {
          return {
            team: parts[0] || "Unknown",
            league: parts.slice(1).join(sep).trim() || "Unknown",
          };
        }
      }
    }

    return { team: s, league: "Unknown" };
  }

  function getTeamLeague(infoFields = {}, apiTeam = "") {
    const teamField = cleanValue(infoFields["Team"], "");
    const leagueField = cleanValue(infoFields["League"], "");

    const combined = cleanValue(
      infoFields["Team / League"] || infoFields["Team/League"],
      ""
    );

    const split = combined ? splitTeamLeague(combined) : { team: "Unknown", league: "Unknown" };

    const team = cleanValue(teamField || apiTeam || split.team, "Unknown");
    const league = cleanValue(leagueField || split.league, "Unknown");

    return { team, league };
  }

  // ---------- UI render components ----------
  function renderCardTable(title, rows) {
    return `
      <section class="space-y-2">
        <h3>${escapeHtml(title)}</h3>
        <div class="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
          <div class="overflow-auto">
            <table class="w-full text-sm border-collapse">
              <tbody>
                ${rows
                  .map(
                    (r, idx) => `
                      <tr class="${idx === 0 ? "" : "border-t border-zinc-200"}">
                        <td class="py-2 pr-4 text-zinc-500 whitespace-normal sm:whitespace-nowrap align-top">${escapeHtml(r.key)}</td>
                        <td class="py-2 font-medium whitespace-normal sm:whitespace-nowrap">${escapeHtml(cleanValue(r.val, "Unknown"))}</td>
                      </tr>
                    `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    `;
  }

  // Metric columns same width across stat tables
  function renderStatCard(title, headers, rows, note, opts = {}) {
    const firstColPx = Number.isFinite(opts.firstColPx) ? opts.firstColPx : 220;
    const metricColPx = Number.isFinite(opts.metricColPx) ? opts.metricColPx : 88;

    const align = (idx) => (idx === 0 ? "text-left" : "text-right");

    // Adapt column widths for small viewports to avoid forcing horizontal scroll
    let effFirst = firstColPx;
    let effMetric = metricColPx;
    try {
      const w = (typeof window !== 'undefined' && window.innerWidth) ? window.innerWidth : 1024;
      if (w < 420) {
        effFirst = Math.min(120, firstColPx);
        effMetric = Math.min(56, metricColPx);
      } else if (w < 640) {
        effFirst = Math.min(160, firstColPx);
        effMetric = Math.min(72, metricColPx);
      }
    } catch (e) {}

    const colgroup = `
      <colgroup>
        <col style="width:${effFirst}px" />
        ${headers.slice(1).map(() => `<col style="width:${effMetric}px" />`).join("")}
      </colgroup>
    `;

    return `
      <section class="space-y-2">
        <h3>${escapeHtml(title)}</h3>
        <div class="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
          <div class="custom-scrollbar" style="overflow-x: auto; -webkit-overflow-scrolling: touch;">
            <table class="text-sm border-collapse" style="table-layout:fixed; min-width: 100%; white-space: nowrap;">
              ${colgroup}
              <thead>
                <tr class="border-b border-zinc-200">
                  ${headers
                    .map(
                      (h, idx) => `
                        <th class="py-2 ${idx === 0 ? "pr-4" : "pl-4"} ${align(idx)} text-zinc-500 font-medium">
                          ${escapeHtml(h)}
                        </th>`
                    )
                    .join("")}
                </tr>
              </thead>
              <tbody>
                ${rows
                  .map(
                    (r) => `
                      <tr class="border-t border-zinc-200">
                        ${r
                          .map(
                            (cell, idx) => `
                              <td class="py-2 ${idx === 0 ? "pr-4" : "pl-4"} ${align(idx)} font-medium" style="${
                                idx === 0 ? "" : "font-variant-numeric: tabular-nums;"
                              }">
                                ${escapeHtml(cleanValue(cell, "—"))}
                              </td>`
                          )
                          .join("")}
                      </tr>`
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
          ${note ? `<div class="mt-2 text-xs text-zinc-500">${escapeHtml(String(note).trim())}</div>` : ""}
        </div>
      </section>
    `;
  }

  // ---------- Renderers using SERVER data ----------
  function renderSeasonSnapshotTable(seasonSnapshot = {}) {
    const games = cleanValue(seasonSnapshot.games, "—");
    const pts = cleanValue(seasonSnapshot.pts, "—");
    const reb = cleanValue(seasonSnapshot.reb, "—");
    const ast = cleanValue(seasonSnapshot.ast, "—");
    const fg = cleanValue(seasonSnapshot.fg, "—");
    const note = (seasonSnapshot.note || "").trim();

    return renderStatCard(
      "Season Snapshot",
      ["GAMES", "PTS", "REB", "AST", "FG"],
      [[games, pts, reb, ast, fg]],
      note,
      { firstColPx: 220, metricColPx: 88 }
    );
  }

  function renderLast3GamesTable(last3 = []) {
    const cleaned = (Array.isArray(last3) ? last3 : []).filter((g) => {
      const opp = String(g?.opp ?? "").trim().toUpperCase();
      const pts = String(g?.pts ?? "").trim().toUpperCase();
      return !(opp === "OPP" || pts === "PTS");
    });

    const rows =
      cleaned.length
        ? cleaned.slice(0, 3).map((g) => [
            cleanValue(g.opp, "—"),
            cleanValue(g.pts, "—"),
            cleanValue(g.reb, "—"),
            cleanValue(g.ast, "—"),
            cleanValue(g.fg, "—"),
          ])
        : [
            ["—", "—", "—", "—", "—"],
            ["—", "—", "—", "—", "—"],
            ["—", "—", "—", "—", "—"],
          ];

    const note = cleaned.length ? "" : "No last-3-games data found in the report.";

    return renderStatCard(
      "Last 3 Games",
      ["OPP", "PTS", "REB", "AST", "FG"],
      rows,
      note,
      { firstColPx: 220, metricColPx: 88 }
    );
  }

  function renderInfoTable(infoFields = {}, apiTeam = "") {
    const { team, league } = getTeamLeague(infoFields, apiTeam);

    const rows = [
      { key: "Team", val: team },
      { key: "League", val: league },
      { key: "Position", val: infoFields["Position"] || "Unknown" },
      { key: "Nationality", val: infoFields["Nationality"] || "Unknown" },
      { key: "Height", val: infoFields["Height"] || "Unknown" },
      { key: "Weight", val: infoFields["Weight"] || "Unknown" },
      { key: "Age / Birthdate", val: infoFields["Age / Birthdate"] || "Unknown" },
      { key: "Dominant hand", val: infoFields["Dominant Hand"] || "Unknown" },
    ];

    return renderCardTable("Info", rows);
  }

  function renderGradesTable(grades = []) {
    const ORDER = [
      "Shooting",
      "Finishing",
      "Playmaking",
      "Handle",
      "Defense",
      "Rebounding",
      "Athleticism",
      "IQ / Decision-making",
    ];

    const toBucket = (skillRaw) => {
      const s = String(skillRaw || "").toLowerCase();
      if (s.includes("shoot")) return "Shooting";
      if (s.includes("finish") || s.includes("rim") || s.includes("paint")) return "Finishing";
      if (s.includes("playmak") || s.includes("pass") || s.includes("creation")) return "Playmaking";
      if (s.includes("handle") || s.includes("ball")) return "Handle";
      if (s.includes("defen") || s.includes("poa") || s.includes("contain")) return "Defense";
      if (s.includes("rebound")) return "Rebounding";
      if (s.includes("athlet") || s.includes("burst") || s.includes("speed") || s.includes("vertical")) return "Athleticism";
      if (s.includes("iq") || s.includes("decision") || s.includes("awareness")) return "IQ / Decision-making";
      return null;
    };

    const canonical = {};
    const extras = [];

    for (const g of grades || []) {
      const bucket = toBucket(g?.skill);
      const scoreNum = Number(g?.score);
      if (bucket && Number.isFinite(scoreNum)) canonical[bucket] = scoreNum;
      else if (g && g.skill) extras.push({ skill: g.skill, score: scoreNum });
    }

    const rows = ORDER.map((skill) => {
      const v = canonical[skill];
      const display = Number.isFinite(v) ? `${v.toFixed(1)}/5` : "—";
      return { key: skill, val: display };
    });

    for (const r of extras.slice(0, 6)) {
      const v = Number(r.score);
      const display = Number.isFinite(v) ? `${v.toFixed(1)}/5` : "—";
      rows.push({ key: r.skill, val: display });
    }

    return renderCardTable("Grades", rows);
  }

  // Enable horizontal drag-to-scroll on overflow-auto containers
  function enableTableDragScroll() {
    // Select all scrollable containers (both with .overflow-auto class and inline overflow-x: auto styles)
    const containers = document.querySelectorAll('.overflow-auto, .custom-scrollbar');
    
    containers.forEach((container) => {
      // Only initialize if not already initialized and if container is actually scrollable
      if (container._dragScrollInitialized) return;
      if (container.scrollWidth <= container.clientWidth) return; // Skip if no horizontal scroll
      
      container._dragScrollInitialized = true;

      let isDown = false;
      let startX;
      let scrollLeft;

      container.addEventListener('mousedown', (e) => {
        // Only activate on left mouse button
        if (e.button !== 0) return;
        isDown = true;
        startX = e.pageX - container.offsetLeft;
        scrollLeft = container.scrollLeft;
        container.style.cursor = 'grabbing';
      });

      container.addEventListener('mouseleave', () => {
        isDown = false;
        container.style.cursor = 'default';
      });

      container.addEventListener('mouseup', () => {
        isDown = false;
        container.style.cursor = 'default';
      });

      container.addEventListener('mousemove', (e) => {
        if (!isDown) return;
        e.preventDefault();
        const x = e.pageX - container.offsetLeft;
        const walk = (x - startX) * 1;
        container.scrollLeft = scrollLeft - walk;
      });

      // Touch support for mobile
      let touchStartX;
      let touchScrollLeft;

      container.addEventListener('touchstart', (e) => {
        if (e.touches.length !== 1) return;
        touchStartX = e.touches[0].pageX - container.offsetLeft;
        touchScrollLeft = container.scrollLeft;
      });

      container.addEventListener('touchmove', (e) => {
        if (!touchStartX) return;
        const x = e.touches[0].pageX - container.offsetLeft;
        const walk = (x - touchStartX) * 1;
        container.scrollLeft = touchScrollLeft - walk;
      });

      container.addEventListener('touchend', () => {
        touchStartX = null;
      });
    });
  }

  function renderReportTitle(player, infoFields = {}, apiTeam = "") {
    // Show only the player's name in the title (avoid duplicating "Scouting Report —" text)
    return `${player}`;
  }

  function renderReport(payload) {
    const player = cleanValue(
      payload.player || payload.player_name || (payload.info_fields && payload.info_fields["Player"]),
      "Player"
    );
    const infoFields = payload.info_fields || {};
    const grades = payload.grades || [];
    const finalVerdict = cleanValue(payload.final_verdict || "", "");

    const seasonSnapshot = payload.season_snapshot || {};
    const last3Games = payload.last3_games || [];

    const reportHtml = payload.report_html || ""; // server-sanitized
    const reportMdFallback = payload.report_md || "";

    const title = renderReportTitle(player, infoFields, payload.team || "");
    const verdictBlock = finalVerdict
      ? `<div class="text-sm text-zinc-700">${escapeHtml(finalVerdict)}</div>`
      : "";

    // Format the generated date and time
    let dateBadge = "";
    if (payload.created_at) {
      try {
        const d = new Date(payload.created_at);
        const dateStr = d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
        const timeStr = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        dateBadge = `<div class="timestamp-badge generated">Generated ${dateStr} ${timeStr}</div>`;
      } catch (e) {
        // Fallback if date parsing fails
        dateBadge = `<div class="timestamp-badge generated">Generated ${payload.created_at.split('T')[0]}</div>`;
      }
    }

    // Stats badge
    let statsBadge = "";
    const statsUpdatedAt = payload.stats_updated_at || payload.updated_at || payload.created_at;
    const reportGeneratedAt = payload.report_generated_at || payload.created_at;
    const statsWasRefreshed = !!payload.stats_updated_at && statsUpdatedAt && reportGeneratedAt && statsUpdatedAt !== reportGeneratedAt;
    const statsBadgeLabel = statsWasRefreshed ? "Stats updated" : "Stats generated";
    // Show badge whenever we have a timestamp (initial generation or refresh)
    if (statsUpdatedAt) {
      try {
        const u = new Date(statsUpdatedAt);
        const uDate = u.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
        const uTime = u.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        statsBadge = `<div class="timestamp-badge stats-updated">${statsBadgeLabel} ${uDate} ${uTime}</div>`;
      } catch (e) {
        console.error('[renderReport] Badge date formatting error:', e);
        statsBadge = `<div class="timestamp-badge stats-updated">${statsBadgeLabel}</div>`;
      }
    }

    // Store player/team for regenerate action
    const playerName = player;
    const playerTeam = cleanValue(infoFields["Team"] || payload.team || "", "");
    const reportId = payload.report_id || payload.library_id || "";

    return `
      <div class="space-y-4">
        <div>
          <div class="text-2xl font-bold text-zinc-900">${escapeHtml(title)}</div>
          <div class="flex flex-wrap gap-2">
            ${dateBadge}
          </div>
        </div>
        
        <div class="space-y-1">
          ${verdictBlock}
        </div>

          <div class="grid sm:grid-cols-2 gap-4">
          ${renderInfoTable(infoFields, payload.team || "")}
          ${renderGradesTable(grades)}
        </div>

        <section>
          <div class="leading-relaxed break-words text-sm
            [&_h1]:text-xl [&_h1]:font-bold [&_h1]:mt-4 [&_h1]:mb-2
            [&_h2]:text-lg [&_h2]:font-bold [&_h2]:mt-4 [&_h2]:mb-2
            [&_h3]:text-base [&_h3]:font-semibold [&_h3]:mt-3 [&_h3]:mb-1
            [&_p]:my-2
            [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:my-2
            [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:my-2
            [&_li]:my-1
            [&_hr]:my-4 [&_hr]:opacity-30
            [&_pre]:my-3 [&_pre]:p-3 [&_pre]:rounded-md [&_pre]:overflow-auto [&_pre]:bg-zinc-100
            [&_code]:font-mono">
            ${reportHtml || (reportMdFallback ? `<pre class="text-xs whitespace-pre-wrap break-words">${escapeHtml(reportMdFallback)}</pre>` : `<span class="text-zinc-500">(empty)</span>`)}
          </div>
        </section>

        <hr class="border-t border-zinc-200 my-6" />
        
        <section class="space-y-3">
          <div>
            <h2 class="text-lg font-semibold text-zinc-900">Brief Stats</h2>
            ${statsBadge ? `<div class="mt-1">${statsBadge}</div>` : ''}
          </div>
          
          ${renderSeasonSnapshotTable(seasonSnapshot)}
          ${renderLast3GamesTable(last3Games)}
        </section>

        <section class="space-y-2">
          <div class="flex flex-col sm:flex-row gap-2">
            <button id="update_stats_btn" class="w-full sm:w-auto text-xs px-3 py-2 rounded-md bg-zinc-100 hover:bg-zinc-200 active:bg-zinc-300 text-zinc-700 font-medium transition-colors shadow-sm" title="Refresh player stats with latest data">
              📋 Update stats
            </button>
            <button id="regenerate_report_btn" class="w-full sm:w-auto text-xs px-3 py-2 rounded-md bg-zinc-100 hover:bg-zinc-200 active:bg-zinc-300 text-zinc-700 font-medium transition-colors shadow-sm" data-player="${escapeHtml(playerName)}" data-team="${escapeHtml(playerTeam)}" data-report-id="${escapeHtml(reportId)}">
              🔄 Regenerate report
            </button>
            <button id="download_pdf_btn" class="w-full sm:w-auto text-xs px-3 py-2 rounded-md bg-zinc-100 hover:bg-zinc-200 active:bg-zinc-300 text-zinc-700 font-medium transition-colors shadow-sm" title="Download report as PDF">
              ⬇️ Download PDF
            </button>  
          </div>
        </section>
      </div>
    `;
  }

  // ---------- App initialization ----------
  function initApp() {
    // prevent page reload on submit
    const form = $("scout-form");
    if (form) form.addEventListener("submit", (e) => e.preventDefault());

    // Handle regenerate button clicks with event delegation
    const outHtml = $("out_html");
    if (outHtml) {
      // Handle Update Stats button
      outHtml.addEventListener("click", (e) => {
        const btn = e.target.closest("#update_stats_btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        (async () => {
          try {
            // Try to get player name from multiple sources
            const playerFromTitle = document.querySelector("#out_html .text-2xl.font-bold")?.textContent || "";
            const player = window._currentReportData?.player || playerFromTitle || "Unknown";
            const team = window._currentReportData?.team || "";
            const league = window._currentReportData?.league || "";
            const reportId = window._regenerateReportId;
            
            // Always fetch fresh balance from server
            log.info("STATS_REFRESH", "Fetching current credits balance");
            let balance = "—";
            const token = await (window.getAccessToken ? window.getAccessToken() : null);
            if (token) {
              try {
                const res = await fetch('/api/credits', {
                  headers: { Authorization: `Bearer ${token}` }
                });
                if (res.ok) {
                  const data = await res.json();
                  balance = data.credits_remaining ?? data.credits ?? "—";
                  log.info("STATS_REFRESH", "Current balance fetched", balance);
                } else {
                  log.warn("STATS_REFRESH", "Failed to fetch balance", res.status);
                }
              } catch (err) {
                log.error("STATS_REFRESH", "Error fetching balance", err);
              }
            }
            
            log.info("STATS_REFRESH", "Opening modal", { player, team, league, reportId, balance });
            
            // Show stats refresh modal
            const statsModal = document.getElementById("stats_refresh_modal");
            const statsPlayerName = document.getElementById("stats_player_name");
            const statsAgeText = document.getElementById("stats_age_text");
            const statsBalance = document.getElementById("stats_refresh_balance");
            const yesBtn = document.getElementById("stats_refresh_yes");
            const noBtn = document.getElementById("stats_refresh_no");
            const closeBtn = document.getElementById("close_stats_refresh_modal");
            
            if (statsPlayerName) statsPlayerName.textContent = player;
            if (statsAgeText) statsAgeText.textContent = "outdated";
            if (statsBalance) statsBalance.textContent = balance;
            
            // Check if enough time has passed since last update (20 second threshold)
            const STATS_REFRESH_THRESHOLD_MS = 20000; // 20 seconds
            const createdAtTime = window._currentReportData?.created_at ? new Date(window._currentReportData.created_at).getTime() : 0;
            const statsUpdatedTime = window._currentReportData?.stats_updated_at ? new Date(window._currentReportData.stats_updated_at).getTime() : createdAtTime;
            const now = Date.now();
            const deltaMs = now - statsUpdatedTime;
            const canRefresh = deltaMs >= STATS_REFRESH_THRESHOLD_MS;
            const secondsRemaining = Math.ceil((STATS_REFRESH_THRESHOLD_MS - deltaMs) / 1000);
            
            // Disable button if threshold not met
            if (!canRefresh) {
              yesBtn.disabled = true;
              yesBtn.classList.add("opacity-50", "cursor-not-allowed");
              const countdownEl = document.createElement("div");
              countdownEl.className = "text-xs text-zinc-600 mt-2";
              countdownEl.textContent = `Wait ${secondsRemaining}s before refreshing`;
              yesBtn.parentElement.appendChild(countdownEl);
            } else {
              yesBtn.disabled = false;
              yesBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
            
            if (statsModal) statsModal.classList.remove("hidden");
            
            // Wait for user response
            const userChoice = await new Promise((resolve) => {
              const handleYes = () => {
                cleanup();
                resolve(true);
              };
              const handleNo = () => {
                cleanup();
                resolve(false);
              };
              const cleanup = () => {
                yesBtn.removeEventListener("click", handleYes);
                noBtn.removeEventListener("click", handleNo);
                closeBtn?.removeEventListener("click", handleNo);
                statsModal.classList.add("hidden");
              };
              
              yesBtn.addEventListener("click", handleYes);
              noBtn.addEventListener("click", handleNo);
              if (closeBtn) closeBtn.addEventListener("click", handleNo);
            });
            
            if (userChoice) {
              // User confirmed - make scout request with refresh_stats=true and report_id to update in place
              log.info("STATS_REFRESH", "User confirmed, starting refresh", { player, reportId });
              
              // Show loader in Brief Stats section
              const briefStatsHeading = Array.from(document.querySelectorAll("#out_html h2")).find(h => h.textContent.includes("Brief Stats"));
              let statsContainer = briefStatsHeading ? briefStatsHeading.closest("section") : null;
              
              if (statsContainer) {
                // Replace with loader
                statsContainer.innerHTML = `
                  <div class="flex items-center justify-between">
                    <h2 class="text-lg font-semibold text-zinc-900">Brief Stats</h2>
                  </div>
                  <div class="flex items-center justify-center py-12">
                    <div class="flex items-center gap-2">
                      <svg class="animate-spin h-6 w-6 text-[#0E2018]" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                      </svg>
                      <div class="text-sm text-zinc-700">Updating stats...</div>
                    </div>
                  </div>
                `;
                log.info("STATS_REFRESH", "Loader shown in Brief Stats section");
              } else {
                log.warn("STATS_REFRESH", "Brief Stats section not found");
              }
              
              const token = await (window.getAccessToken ? window.getAccessToken() : null);
              log.info("STATS_REFRESH", "Auth token obtained", !!token);
              
              const response = await fetch("/api/scout", {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  ...(token ? { Authorization: `Bearer ${token}` } : {}),
                },
                body: JSON.stringify({
                  player: player,
                  team: team,
                  league: league,
                  refresh_stats: true,  // Only refresh stats, not full report
                  report_id: reportId  // Update this specific report
                }),
              });
              
              log.info("STATS_REFRESH", "Response received", { status: response.status });
              const data = await response.json().catch(() => ({}));
              log.info("STATS_REFRESH", "Response parsed", { 
                ok: response.ok,
                credits_remaining: data.credits_remaining,
                report_id: data.report_id,
                keys: Object.keys(data).slice(0, 10)
              });
              
              if (!response.ok) {
                log.error("STATS_REFRESH", "Request failed", data.error);
                throw new Error(data.error || `Request failed (${response.status})`);
              }
              
              // Render the refreshed report
              log.info("STATS_REFRESH", "Rendering updated report", { has_report_html: !!data.report_html, report_html_len: data.report_html?.length });
              const renderedHtml = renderReport(data);
              log.info("STATS_REFRESH", "Report rendered", { html_len: renderedHtml?.length });
              $("out_html").innerHTML = renderedHtml;
              window.enableTableDragScroll?.();
              
              // Update stored data
              window._currentReportData = {
                player: data.player || data.player_name || "Unknown",
                team: data.team || "",
                league: data.league || ""
              };
              window._currentCreditsBalance = data.credits_remaining;
              log.info("STATS_REFRESH", "Credits balance updated", data.credits_remaining);
              
              // Update credits display in speech bubble immediately
              if (typeof data.credits_remaining === "number") {
                log.info("STATS_REFRESH", "Updating credits bubble display", data.credits_remaining);
                window.updateCreditsDisplay?.(data.credits_remaining);
              } else {
                log.warn("STATS_REFRESH", "credits_remaining missing from response", typeof data.credits_remaining);
              }
              
              // Close the modal to show the updated report
              const modal = document.getElementById("stats_modal");
              if (modal) {
                modal.classList.add("hidden");
                log.info("STATS_REFRESH", "Modal closed");
              }
              
              log.success("STATS_REFRESH", "Stats refresh complete");
            }
          } catch (err) {
            console.error("Error handling update stats button:", err);
            setText("status", "");
            setText("err", err?.message || "Failed to update stats");
          }
        })();
      });

      // Handle Regenerate button - open modal instead of immediately regenerating
      outHtml.addEventListener("click", (e) => {
        const btn = e.target.closest("#regenerate_report_btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        (async () => {
          try {
            const reportId = btn.getAttribute("data-report-id");
            
            // Always fetch fresh balance from server
            log.info("REGENERATE", "Fetching current credits balance");
            let balance = "—";
            const token = await (window.getAccessToken ? window.getAccessToken() : null);
            if (token) {
              try {
                const res = await fetch('/api/credits', {
                  headers: { Authorization: `Bearer ${token}` }
                });
                if (res.ok) {
                  const data = await res.json();
                  balance = data.credits_remaining ?? data.credits ?? "—";
                  log.info("REGENERATE", "Current balance fetched", balance);
                } else {
                  log.warn("REGENERATE", "Failed to fetch balance", res.status);
                }
              } catch (err) {
                log.error("REGENERATE", "Error fetching balance", err);
              }
            }
            
            // Store data for confirmation
            window._pendingRegenerateData = {
              player: btn.getAttribute("data-player"),
              team: btn.getAttribute("data-team"),
              reportId: reportId
            };
            
            // Show regenerate modal
            const regenerateModal = document.getElementById("regenerate_modal");
            const regenerateBalance = document.getElementById("regenerate_balance");
            
            if (regenerateBalance) regenerateBalance.textContent = balance;
            if (regenerateModal) regenerateModal.classList.remove("hidden");
          } catch (err) {
            console.error("Error handling regenerate button:", err);
          }
        })();
      });

      // Handle PDF download button clicks
      outHtml.addEventListener("click", (e) => {
        const btn = e.target.closest("#download_pdf_btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        (async () => {
          try {
            // Get report ID from stored variable or extract from the closest regenerate button
            let reportId = window._regenerateReportId;
            if (!reportId) {
              // Try to get it from the regenerate button's data attribute
              const regenerateBtn = document.getElementById("regenerate_report_btn");
              if (regenerateBtn) {
                reportId = regenerateBtn.getAttribute("data-report-id");
              }
            }
            
            if (!reportId) {
              alert("No report to download. Generate a report first.");
              return;
            }
            
            // Show loader and disable button
            const originalText = btn.textContent;
            btn.disabled = true;
            btn.textContent = "⏳ Compiling…";
            
            const token = await (window.getAccessToken ? window.getAccessToken() : null);
            const response = await fetch(`/api/reports/${reportId}/pdf`, {
              headers: token ? { Authorization: `Bearer ${token}` } : {}
            });
            if (!response.ok) {
              throw new Error(`Download failed: ${response.status}`);
            }
            const disposition = response.headers.get('Content-Disposition') || '';
            const match = disposition.match(/filename="?([^";]+)"?/i);
            const filename = (match && match[1]) ? match[1] : `scout_report_${reportId}.pdf`;
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
            window.trackClientEvent?.('report_downloaded', { report_id: reportId });
            
            // Restore button
            btn.disabled = false;
            btn.textContent = originalText;
          } catch (err) {
            console.error('PDF download failed:', err);
            alert('Failed to download PDF: ' + (err.message || 'Unknown error'));
            // Restore button
            btn.disabled = false;
            btn.textContent = originalText;
          }
        })();
      });
    }

    // Modal handlers using event delegation to prevent listener stacking
    document.addEventListener('click', (e) => {
      // Close regenerate modal button (handle SVG clicks too)
      const closeBtn = e.target.closest('#close_regenerate_modal');
      if (closeBtn) {
        log.info("REGENERATE", "Close button clicked");
        const modal = document.getElementById('regenerate_modal');
        if (modal) modal.classList.add('hidden');
        return;
      }

      // Cancel regenerate button
      if (e.target.id === 'regenerate_cancel') {
        const modal = document.getElementById('regenerate_modal');
        if (modal) modal.classList.add('hidden');
        return;
      }

      // Confirm regenerate button
      if (e.target.id === 'regenerate_confirm') {
        const modal = document.getElementById('regenerate_modal');
        if (modal) modal.classList.add('hidden');
        
        // Execute regeneration
        const data = window._pendingRegenerateData;
        if (!data) {
          console.warn('No regenerate data found');
          return;
        }
        
        try {
          // Store report_id for the regeneration request (from pending data)
          window._regenerateReportId = data.reportId || null;
          
          // Set a flag to force refresh on next scout request
          window._forceRefresh = true;

          // Fill the form with these values
          if ($("player")) $("player").value = data.player;
          if ($("team")) $("team").value = data.team;

          // Trigger the run button click to start generation
          const runBtn = $("run");
          if (runBtn) {
            runBtn.click();
          }
        } catch (err) {
          console.error("Error confirming regenerate:", err);
        }
        return;
      }

      // Close modal when clicking outside (regenerate modal overlay)
      if (e.target.id === 'regenerate_modal') {
        e.target.classList.add('hidden');
        return;
      }
    });

    // Run
    function setScoutLoader(show) {
      try {
        const l = document.getElementById('report_loader');
        if (!l) return;
        if (show) l.classList.remove('hidden'); else l.classList.add('hidden');
      } catch (e) {}
    }

    on("run", "click", async () => {
      if (window._scoutRunning) return; // prevent duplicate concurrent runs
      window._scoutRunning = true;
      try {
        setText("err");
        setText("badge");
      } catch (err) {
        console.error('Error preparing run handler UI', err);
      }

      const player = $("player").value.trim();
      if (!player) {
        setText("err", "Player is required.");
        window._scoutRunning = false;
        $("run").disabled = false;
        setScoutLoader(false);
        return;
      }

      const payload = {
        player,
        team: $("team")?.value?.trim() || "",
        league: $("league")?.value?.trim() || "",
        season: $("season")?.value?.trim() || "",
        use_web: !!$("use_web")?.checked,
        refresh: !!$("refresh")?.checked || !!window._forceRefresh,
      };
      
      // Clear force refresh flag after using it
      if (window._forceRefresh) {
        window._forceRefresh = false;
      }

      // If regenerating an existing report, include its ID so the backend updates it
      if (window._regenerateReportId) {
        payload.report_id = window._regenerateReportId;
        window._regenerateReportId = null; // Clear it after use
      }

      // Store button text to preserve it after disabling/enabling
      const runBtn = $("run");
      if (runBtn) {
        runBtn.dataset.originalText = runBtn.textContent;
      }

      setText("status", "Working…");
      $("run").disabled = true;
      // Clear the report pane completely before generating new report
      $("out_html").innerHTML = "";
      setScoutLoader(true);

      try {
        const token = await (window.getAccessToken ? window.getAccessToken() : null);
        const res = await fetch("/api/scout", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

        // Show notification if stats were auto-refreshed
        if (data.stats_refreshed) {
          showToast("Stats updated with latest data", "success");
        }

        // If stats are stale, show confirmation dialog before refreshing
        if (data && data.stats_stale) {
          const ageMinutes = Math.floor(data.stats_age_seconds / 60);
          const ageHours = Math.floor(ageMinutes / 60);
          const ageDays = Math.floor(ageHours / 24);
          let ageText = "";
          if (ageDays > 0) {
            ageText = `${ageDays} day${ageDays > 1 ? 's' : ''} old`;
          } else if (ageHours > 0) {
            ageText = `${ageHours} hour${ageHours > 1 ? 's' : ''} old`;
          } else {
            ageText = `${ageMinutes} minute${ageMinutes > 1 ? 's' : ''} old`;
          }
          
          // Show custom modal instead of native confirm
          const statsModal = $("stats_refresh_modal");
          const statsPlayerName = $("stats_player_name");
          const statsAgeText = $("stats_age_text");
          const statsBalance = $("stats_refresh_balance");
          const yesBtn = $("stats_refresh_yes");
          const noBtn = $("stats_refresh_no");
          const closeBtn = $("close_stats_refresh_modal");
          
          if (statsModal && statsAgeText && yesBtn && noBtn) {
            // Set player name
            if (statsPlayerName) {
              statsPlayerName.textContent = data.player || data.player_name || "This player";
            }
            
            // Set age text
            statsAgeText.textContent = ageText;
            
            // Set balance
            if (statsBalance) {
              statsBalance.textContent = data.credits_remaining !== undefined ? data.credits_remaining : "—";
            }
            
            // Show modal
            statsModal.classList.remove("hidden");
            
            // Wait for user response
            const userChoice = await new Promise((resolve) => {
              const handleYes = () => {
                cleanup();
                resolve(true);
              };
              const handleNo = () => {
                cleanup();
                resolve(false);
              };
              const cleanup = () => {
                yesBtn.removeEventListener("click", handleYes);
                noBtn.removeEventListener("click", handleNo);
                closeBtn?.removeEventListener("click", handleNo);
                statsModal.classList.add("hidden");
              };
              
              yesBtn.addEventListener("click", handleYes);
              noBtn.addEventListener("click", handleNo);
              if (closeBtn) closeBtn.addEventListener("click", handleNo);
            });
            
            if (userChoice) {
              // User confirmed - make new request with refresh_stats=true
              setText("status", "Updating stats…");
              const tokenRefresh = await (window.getAccessToken ? window.getAccessToken() : null);
              const resRefresh = await fetch("/api/scout", {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  ...(tokenRefresh ? { Authorization: `Bearer ${tokenRefresh}` } : {}),
                },
                body: JSON.stringify({
                  ...payload,
                  refresh_stats: true,
                }),
              });
              const dataRefresh = await resRefresh.json().catch(() => ({}));
              if (!resRefresh.ok) throw new Error(dataRefresh.error || `Stats refresh failed (${resRefresh.status})`);
              
              // Replace data with refreshed version
              Object.assign(data, dataRefresh);
              delete data.stats_stale; // Clear the flag since we just refreshed
            } else {
              // User declined - just show the stale report
              delete data.stats_stale; // Clear the flag so we don't loop
            }
          }
        }

        // If server suggests a close cached match, show modal
        if (data && data.match_suggestion) {
          try {
            const ms = data.match_suggestion;
            
            // Build suggestion text
            let suggestionText = `Did you mean ${ms.player_name}`;
            if (ms.team) {
              suggestionText += ` (${ms.team}`;
              if (ms.league) suggestionText += `, ${ms.league}`;
              suggestionText += ")";
            } else if (ms.league) {
              suggestionText += ` (${ms.league})`;
            }
            suggestionText += "?";
            
            // Store suggestion data for event handlers
            window._pendingSuggestion = {
              report_id: ms.report_id,
              player_name: ms.player_name,
              player_query: player,
              payload_data: payload
            };
            
            // Show modal
            const modal = $("suggestion_modal");
            const text = $("suggestion_text");
            if (modal && text) {
              text.textContent = suggestionText;
              modal.classList.remove("hidden");
              setText("status", "");
            }
          } catch (err) {
            console.error('Error handling match_suggestion', err);
          }
          // Always return when match_suggestion is present - don't render as report
          return;
        }

        // Update credits display if server returned it
        if (typeof data.credits_remaining === "number") {
          try {
            window.updateCreditsDisplay?.(data.credits_remaining);
          } catch (err) {
            console.warn('Failed to update credits display', err);
          }
        }

        // If this was a cached/library hit but structured fields are missing,
        // fetch the canonical report endpoint to ensure tables (season snapshot,
        // grades, last3_games) are present. This handles cases where the
        // quick /api/scout response only includes `report_html`.
        const needsFetch = data && data.cached && data.report_id && (!data.info_fields || !data.grades || !data.last3_games);
        if (needsFetch) {
          try {
            const tokenR = await (window.getAccessToken ? window.getAccessToken() : null);
            const rr = await fetch(`/api/reports/${data.report_id}`, { headers: { ...(tokenR ? { Authorization: `Bearer ${tokenR}` } : {}) } });
            if (rr.ok) {
              const fuller = await rr.json().catch(() => null);
              if (fuller) {
                data = Object.assign({}, data, fuller);
              }
            }
          } catch (err) {
            console.warn('failed to fetch full report for tables', err);
          }
        }

        // If server didn't provide rendered HTML but we have markdown, request
        // server-side rendering so UI shows the same sanitized HTML as library
        // view. Fall back to raw markdown if render endpoint fails.
        if ((!data.report_html || data.report_html === "") && data.report_md) {
          try {
            const tokenR = await (window.getAccessToken ? window.getAccessToken() : null);
            const rr = await fetch('/api/render_md', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                ...(tokenR ? { Authorization: `Bearer ${tokenR}` } : {}),
              },
              body: JSON.stringify({ md: data.report_md }),
            });
            if (rr.ok) {
              const jr = await rr.json().catch(() => ({}));
              if (jr && jr.html) data.report_html = jr.html;
            }
          } catch (err) {
            console.warn('render_md call failed', err);
          }
        }

        // render html
        $("out_html").innerHTML = renderReport(data);

        // Store current report data for modal buttons
        window._currentReportData = {
          player: data.player || data.player_name || "Unknown",
          team: data.team || "",
          league: data.league || ""
        };
        window._currentCreditsBalance = data.credits_remaining;
        window._regenerateReportId = data.report_id || data.library_id || data.id;

        // Enable drag-to-scroll on report tables
        window.enableTableDragScroll?.();

        // Show PDF download button if report has an ID
        try {
          const downloadBtn = $("download_pdf_btn");
          if (downloadBtn && data.report_id) {
            downloadBtn.classList.remove("hidden");
          }
        } catch (err) {}

        // Track report_rendered (always, with source indicating where it came from)
        try {
          const source = data.cached ? "cache" : "generated";
          window.trackClientEvent?.("report_rendered", {
            player_name: data.player || data.player_name || "Unknown",
            source: source,
            success: true
          });
        } catch (err) {}

        // Track library report load specifically
        try {
          if (data.cached || data.from_suggestion || data.auto_matched) {
            window.trackClientEvent?.("library_report_loaded", {
              player_name: data.player || data.player_name || "Unknown",
              report_id: data.report_id,
              from_suggestion: data.from_suggestion || false,
              auto_matched: data.auto_matched || false
            });
          }
        } catch (err) {}

        // ✅ refresh sidebar list when report was newly added to user's library
        // Always refresh when not cached, or when it's a suggestion acceptance (adds to library)
        if (!data.cached || data.from_suggestion || data.refreshed) {
          window.loadReports?.();
        }

        // Clear badge
        setText("badge");
        setText("status", "");
      } catch (e) {
        setText("err", e?.message || String(e));
        setText("status");
      } finally {
        window._scoutRunning = false;
        const runBtn = $("run");
        if (runBtn) {
          runBtn.disabled = false;
          // Restore button text from data attribute
          if (runBtn.dataset.originalText) {
            runBtn.textContent = runBtn.dataset.originalText;
          }
        }
        setScoutLoader(false);
      }
    });

    // Reports sidebar: search (click handler is in index.html to avoid duplicates)
    const reportsListEl = $("reports_list");
    const reportsCountEl = $("reports_count");
    const reportSearchEl = $("report_q");

    // Note: Click handler for reports list is in index.html's openReport() function
    // to avoid duplicate requests. Do not add another click listener here.

    let searchTimer = null;
    if (reportSearchEl) {
      reportSearchEl.addEventListener("input", () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          loadReports(reportSearchEl.value || "");
        }, 250);
      });
    }

    window.loadReports = (qArg) => loadReports(qArg ?? (reportSearchEl?.value || ""));

    // Initial load
    loadReports(reportSearchEl?.value || "");

    // Refresh when tab becomes visible again (e.g., after idle)
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        loadReports(reportSearchEl?.value || "");
      }
    });
  }

  // Run after DOM ready (handle script loading before/after DOMContentLoaded)
  function _startApp() {
    try {
      if (!isAppPage()) return; // safe no-op on landing/login/callback/success pages
      initApp();
    } catch (e) {
      console.error('Error starting app', e);
    }
  }

  // Expose functions for other scripts (base.html uses window.renderReport when opening saved reports)
  try { window.renderReport = renderReport; } catch (e) {}
  try { window.enableTableDragScroll = enableTableDragScroll; } catch (e) {}

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _startApp);
  } else {
    _startApp();
  }
})();
