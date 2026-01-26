// static/app.js
"use strict";

const sb = window.sb;

(function () {
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

        $("out_html").innerHTML = renderReport(data);
        window.enableTableDragScroll?.();
        setText("badge");
        setText("status", "");

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
        dateBadge = `<div class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-zinc-100 text-zinc-700">Generated ${dateStr} ${timeStr}</div>`;
      } catch (e) {
        // Fallback if date parsing fails
        dateBadge = `<div class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-zinc-100 text-zinc-700">Generated ${payload.created_at.split('T')[0]}</div>`;
      }
    }

    // Store player/team for regenerate action
    const playerName = player;
    const playerTeam = cleanValue(infoFields["Team"] || payload.team || "", "");
    const reportId = payload.report_id || payload.library_id || "";

    return `
      <div class="space-y-4">
        <div class="space-y-1">
          <div class="text-2xl font-bold text-zinc-900">${escapeHtml(title)}</div>
          ${dateBadge}
          ${verdictBlock}
        </div>

        <div class="flex items-center gap-2">
          <button id="download_pdf_btn" class="text-sm px-3 py-1 rounded-md border border-zinc-300 bg-white hover:bg-zinc-50 text-zinc-700 font-medium transition-colors" title="Download report as PDF">
            ⬇️ Download PDF
          </button>  
          <button id="regenerate_report_btn" class="text-sm px-3 py-1 rounded-md border border-zinc-300 bg-white hover:bg-zinc-50 text-zinc-700 font-medium transition-colors" data-player="${escapeHtml(playerName)}" data-team="${escapeHtml(playerTeam)}" data-report-id="${escapeHtml(reportId)}">
            ✨ Regenerate
          </button>
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

        ${renderSeasonSnapshotTable(seasonSnapshot)}
        ${renderLast3GamesTable(last3Games)}
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
      outHtml.addEventListener("click", (e) => {
        const btn = e.target.closest("#regenerate_report_btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        try {
          // Extract player, team, and report_id from button data attributes
          const player = btn.getAttribute("data-player");
          const team = btn.getAttribute("data-team");
          const reportId = btn.getAttribute("data-report-id");

          if (!player) {
            console.warn("Regenerate button missing player data");
            return;
          }

          // Store report_id for the regeneration request
          window._regenerateReportId = reportId || null;

          // Fill the form with these values
          if ($("player")) $("player").value = player;
          if ($("team")) $("team").value = team;

          // Check the refresh checkbox to force regeneration
          if ($("refresh")) $("refresh").checked = true;

          // Trigger the run button click to start generation
          const runBtn = $("run");
          if (runBtn) runBtn.click();
        } catch (err) {
          console.error("Error handling regenerate button:", err);
        }
      });

      // Handle PDF download button clicks
      outHtml.addEventListener("click", (e) => {
        const btn = e.target.closest("#download_pdf_btn");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();

        (async () => {
          try {
            const reportId = window._regenerateReportId;
            if (!reportId) {
              alert("No report to download. Generate a report first.");
              return;
            }
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
          } catch (err) {
            console.error('PDF download failed:', err);
            alert('Failed to download PDF: ' + (err.message || 'Unknown error'));
          }
        })();
      });
    }

    // Fill example
    on("example", "click", () => {
      $("player").value = "Giannis Antetokounmpo";
      $("team").value = "Milwaukee Bucks";
      $("league").value = " ";
      $("season").value = " ";
      $("use_web").checked = false;
      $("refresh").checked = false;
      setText("badge");
      setText("status");
      setText("err");
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
        refresh: !!$("refresh")?.checked,
      };

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

        // If server suggests a close cached match, show inline suggestion UI
        if (data && data.match_suggestion) {
          try {
            const ms = data.match_suggestion;
            
            // Auto-accept exact matches (score=100) without showing modal
            if (ms.score === 100) {
              try {
                // Record alias mapping
                const tokenAlias = await (window.getAccessToken ? window.getAccessToken() : null);
                await fetch('/api/alias', {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    ...(tokenAlias ? { Authorization: `Bearer ${tokenAlias}` } : {}),
                  },
                  body: JSON.stringify({ queried_player: player, player: ms.player_name }),
                }).catch((err) => { console.warn('alias save failed', err); });
              } catch (err) {
                console.warn('alias endpoint call failed', err);
              }
              
              // Accept exact match automatically
              setText("status", "Loading exact match…");
              const tokenAccept = await (window.getAccessToken ? window.getAccessToken() : null);
              const rAccept = await fetch('/api/scout', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  ...(tokenAccept ? { Authorization: `Bearer ${tokenAccept}` } : {}),
                },
                body: JSON.stringify({
                  player: player,
                  team: $("team")?.value?.trim() || "",
                  league: $("league")?.value?.trim() || "",
                  season: $("season")?.value?.trim() || "",
                  use_web: false,
                  refresh: false,
                  accept_suggestion: true,
                  suggestion_report_id: ms.report_id,
                }),
              });
              
              const acceptResult = await rAccept.json().catch(() => ({}));
              if (!rAccept.ok) {
                throw new Error(acceptResult.error || `Failed to accept suggestion (${rAccept.status})`);
              }
              
              // Mark as from suggestion for clear messaging
              acceptResult.from_suggestion = true;
              
              // Display the result
              $("out_html").innerHTML = renderReport(acceptResult);
              window.enableTableDragScroll?.();
              setText("badge");
              setText("status", "");
              
              // Update credits display
              if (typeof acceptResult.credits_remaining === "number") {
                try {
                  window.updateCreditsDisplay?.(acceptResult.credits_remaining);
                } catch (err) {
                  console.warn('Failed to update credits display', err);
                }
              }
              
              window.loadReports?.();
              return;
            }
            
            // Show inline suggestion box for non-exact matches
            const box = $("suggestion_box");
            const text = $("suggestion_text");
            if (box && text) {
              // Build suggestion text with team/league context
              let suggestionText = `Did you mean ${ms.player_name}`;
              if (ms.team) {
                suggestionText += ` (${ms.team}`;
                if (ms.league) suggestionText += `, ${ms.league}`;
                suggestionText += ")";
              } else if (ms.league) {
                suggestionText += ` (${ms.league})`;
              }
              suggestionText += "?";
              text.textContent = suggestionText;
              box.classList.remove('hidden');

              // create a Promise that resolves to 'accept'|'reject'
              const choice = await new Promise((resolve) => {
                const onAccept = async () => { resolve('accept'); cleanup(); };
                const onReject = async () => { resolve('reject'); cleanup(); };

                function cleanup() {
                  try {
                    $('suggest_accept')?.removeEventListener('click', onAccept);
                    $('suggest_reject')?.removeEventListener('click', onReject);
                  } catch (e) {}
                }

                $('suggest_accept')?.addEventListener('click', onAccept);
                $('suggest_reject')?.addEventListener('click', onReject);
              });

              box.classList.add('hidden');

              if (choice === 'accept') {
                try {
                  // Record alias mapping so future lookups avoid LLM calls
                  const tokenAlias = await (window.getAccessToken ? window.getAccessToken() : null);
                  await fetch('/api/alias', {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/json',
                      ...(tokenAlias ? { Authorization: `Bearer ${tokenAlias}` } : {}),
                    },
                    body: JSON.stringify({ queried_player: player, player: ms.player_name }),
                  }).catch((err) => { console.warn('alias save failed', err); });
                } catch (err) {
                  console.warn('alias endpoint call failed', err);
                }
                
                // Accept suggestion by calling /api/scout with accept_suggestion=true
                // This will charge 1 credit but skip LLM and reuse the cached report
                try {
                  setText("status", "Accepting suggestion…");
                  const tokenAccept = await (window.getAccessToken ? window.getAccessToken() : null);
                  const rAccept = await fetch('/api/scout', {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/json',
                      ...(tokenAccept ? { Authorization: `Bearer ${tokenAccept}` } : {}),
                    },
                    body: JSON.stringify({
                      player: player,
                      team: $("team")?.value?.trim() || "",
                      league: $("league")?.value?.trim() || "",
                      season: $("season")?.value?.trim() || "",
                      use_web: false,
                      refresh: false,
                      accept_suggestion: true,
                      suggestion_report_id: ms.report_id,
                    }),
                  });
                  
                  const acceptResult = await rAccept.json().catch(() => ({}));
                  if (!rAccept.ok) {
                    throw new Error(acceptResult.error || `Failed to accept suggestion (${rAccept.status})`);
                  }
                  
                  // Mark as from suggestion for clear messaging
                  acceptResult.from_suggestion = true;
                  
                  // Display the result (already includes HTML, credit info, etc.)
                  $("out_html").innerHTML = renderReport(acceptResult);
                  window.enableTableDragScroll?.();
                  setText("badge");
                  setText("status", "");
                  
                  // Update credits display
                  if (typeof acceptResult.credits_remaining === "number") {
                    try {
                      window.updateCreditsDisplay?.(acceptResult.credits_remaining);
                    } catch (err) {
                      console.warn('Failed to update credits display', err);
                    }
                  }
                  
                  window.loadReports?.();
                  return;
                } catch (err) {
                  setText("err", err?.message || String(err));
                  setText("status");
                  console.error('Error accepting suggestion:', err);
                }
                return;
              }

              if (choice === 'reject') {
                // redo generation with refresh=true to force LLM call
                payload.refresh = true;
                const token3 = await (window.getAccessToken ? window.getAccessToken() : null);
                const res2 = await fetch("/api/scout", {
                  method: "POST",
                  headers: {
                    "Content-Type": "application/json",
                    ...(token3 ? { Authorization: `Bearer ${token3}` } : {}),
                  },
                  body: JSON.stringify(payload),
                });
                const data2 = await res2.json().catch(() => ({}));
                if (!res2.ok) throw new Error(data2.error || `Request failed (${res2.status})`);
                // replace data with new generation result
                data = data2;
              }
            }
          } catch (err) {
            console.error('Error handling match_suggestion', err);
          }
        }

        // Update credits display if server returned it
        if (typeof data.credits_remaining === "number") {
          try {
            window.updateCreditsDisplay?.(data.credits_remaining);
          } catch (err) {
            console.warn('Failed to update credits display', err);
          }
        }

        // keep markdown hidden for debugging
        setText("out_md", data.report_md || "");

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

    // Reports sidebar: search, click, refresh on visibility
    const reportsListEl = $("reports_list");
    const reportsCountEl = $("reports_count");
    const reportSearchEl = $("report_q");

    if (reportsListEl) {
      reportsListEl.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-report-id]");
        if (!btn) return;
        const rid = btn.getAttribute("data-report-id");
        openReportById(rid);
      });
    }

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
