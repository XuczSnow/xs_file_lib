/**
 * 访问数据看板（独立页 dashboard.html）逻辑。
 *
 * 拉 /api/share-stats（受 token 保护）：总 PV/UV、各链接 PV、TOP 10 IP、按天分布。
 * 数据 = 磁盘已落盘日志 + 内存未落盘缓冲（写批量、读实时）。
 * token 由 common.js 的 authToken（localStorage: xs_token）自动携带；
 * 401 时提示登录失效。核心指标卡 + 近 7 天 CSS 柱状图（数值标签 + 峰值高亮）。
 */
(function () {
    "use strict";

    const X = window.XSLib;
    const esc = X.escapeHtml;

    const $ = (id) => document.getElementById(id);

    // 最近访问时间的格式化（顶栏显示"更新于 HH:MM:SS"）
    function nowStr() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }

    /** 取 YYYY-MM-DD（今天），用于"今日访问"。 */
    function todayKey() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
    }

    /** 近 7 天柱状图：按日期取最后 7 个有访问的日历日（缺日补 0），
     *  渲染为 CSS 柱条，峰值柱高亮，每根柱顶显示数值，X 轴显示 MM-DD。 */
    function renderDaily(daily) {
        const chart = $("dailyChart");
        const today = todayKey();

        // 取最近 7 个日历日（无论是否有访问），保证 X 轴连续
        const days = [];
        const pad = (n) => String(n).padStart(2, "0");
        for (let i = 6; i >= 0; i--) {
            const d = new Date();
            d.setDate(d.getDate() - i);
            const key = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
            days.push({ key, label: pad(d.getMonth() + 1) + "-" + pad(d.getDate()),
                         value: daily[key] || 0 });
        }
        const total = days.reduce((s, x) => s + x.value, 0);
        const maxV = Math.max(1, ...days.map((x) => x.value));
        const peak = days.reduce((a, b) => (b.value > a.value ? b : a));

        $("dailyHint").textContent =
            total > 0 ? "合计 " + total + " 次" : "近 7 天暂无访问记录";

        chart.innerHTML = days.map((d) => {
            const pct = Math.round((d.value / maxV) * 100);
            const isPeak = total > 0 && d.value === peak.value && d.value > 0;
            return `
            <div class="bar-col${isPeak ? " bar-peak" : ""}${d.value ? " bar-has" : ""}"
                 title="${esc(d.label)}：${d.value} 次">
                <span class="bar-val">${d.value || ""}</span>
                <div class="bar-track">
                    <div class="bar-fill" style="height:${Math.max(d.value ? 4 : 0, pct)}%"></div>
                </div>
                <span class="bar-x">${esc(d.label)}</span>
            </div>`;
        }).join("");
    }

    /** 拉取并渲染看板。401 → 显示未授权提示；空数据 → 各表显示占位。 */
    async function load() {
        const $err = $("dashError"), $body = $("dashBody"), $loading = $("dashLoading");
        $loading.style.display = "";
        $body.style.display = "none";
        $err.style.display = "none";

        let s;
        try {
            const res = await fetch("/api/share-stats", { headers: X.authHeaders() });
            if (!res.ok) throw new Error("HTTP " + res.status);
            s = await res.json();
        } catch (e) {
            // 401/403 → 未授权或 token 失效；其余按无数据处理
            $loading.style.display = "none";
            if (e.message === "HTTP 401" || e.message === "HTTP 403") {
                $err.style.display = "flex";
            } else {
                $("dashSub").innerHTML = "站点：<span>—</span>";
                $body.style.display = "block";
                renderEmpty();
            }
            return;
        }

        $("dashUpdated").textContent = "更新于 " + nowStr();

        // 站点名（顶栏副标题）
        try {
            const cfg = await X.getJSON("/api/settings");
            $("dashSite").textContent = cfg.site_name || "（未命名）";
        } catch (e) { /* 站点名保持默认 */ }

        // 核心指标
        $("kpiPv").textContent = s.total_pv;
        $("kpiUv").textContent = s.total_uv;
        $("kpiLinks").textContent = Object.keys(s.per_link || {}).length;
        $("kpiToday").textContent = (s.daily || {})[todayKey()] || 0;

        // 近 7 天趋势
        renderDaily(s.daily || {});

        // TOP 10 IP 表
        $("statTopIp").innerHTML = (s.top_ips && s.top_ips.length)
            ? s.top_ips.map((r) =>
                `<tr><td class="mono">${esc(r.ip)}</td><td class="text-end">${r.count}</td></tr>`
            ).join("")
            : '<tr><td colspan="2" class="text-center text-secondary">暂无数据</td></tr>';

        // 各链接 PV 表
        const perLink = Object.entries(s.per_link || {});
        // 按 PV 降序，便于快速看热点链接
        perLink.sort((a, b) => b[1] - a[1]);
        $("statPerLink").innerHTML = perLink.length
            ? perLink.map(([id, cnt]) =>
                `<tr><td class="mono">${esc(id)}</td><td class="text-end">${cnt}</td></tr>`
            ).join("")
            : '<tr><td colspan="2" class="text-center text-secondary">暂无数据</td></tr>';

        $loading.style.display = "none";
        $body.style.display = "block";
    }

    /** 未授权时的空态（各 KPI 与表格占位）。 */
    function renderEmpty() {
        ["kpiPv", "kpiUv", "kpiLinks", "kpiToday"].forEach((id) => $(id).textContent = "-");
        $("statTopIp").innerHTML = '<tr><td colspan="2" class="text-center text-secondary">暂无数据</td></tr>';
        $("statPerLink").innerHTML = '<tr><td colspan="2" class="text-center text-secondary">暂无数据</td></tr>';
        $("dailyChart").innerHTML = "";
        $("dailyHint").textContent = "暂无访问记录";
    }

    document.addEventListener("DOMContentLoaded", () => {
        $("refreshBtn").addEventListener("click", load);
        load();
    });
})();
