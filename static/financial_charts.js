(function () {
  const data = window.financialCharts || { branches: [], categories: [], trends: [] };
  const palette = ["#0f766e", "#2563eb", "#b7791f", "#b42318", "#6d5dfc", "#047857", "#334155", "#be185d"];
  const sar = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

  function clear(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  function node(name, attrs = {}) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  }

  function text(svg, value, x, y, attrs = {}) {
    const el = node("text", { x, y, ...attrs });
    el.textContent = value;
    svg.appendChild(el);
    return el;
  }

  function showTip(tip, message, event) {
    if (!tip) return;
    tip.textContent = message;
    tip.style.opacity = "1";
    tip.style.left = `${event.offsetX + 14}px`;
    tip.style.top = `${event.offsetY + 14}px`;
  }

  function hideTip(tip) {
    if (tip) tip.style.opacity = "0";
  }

  function renderBranchBars(metric = "exposure") {
    const svg = document.getElementById("branchBarChart");
    if (!svg) return;
    clear(svg);
    const rows = data.branches || [];
    const tip = document.getElementById("branchChartTip");
    if (!rows.length) {
      text(svg, "No branch data yet", 330, 170, { class: "chart-empty" });
      return;
    }
    const margin = { top: 24, right: 30, bottom: 56, left: 78 };
    const width = 760 - margin.left - margin.right;
    const height = 340 - margin.top - margin.bottom;
    const max = Math.max(...rows.map((row) => Number(row[metric]) || 0), 1);
    const slot = width / rows.length;
    const barWidth = Math.min(54, slot * 0.58);

    [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const y = margin.top + height - height * ratio;
      svg.appendChild(node("line", { x1: margin.left, x2: margin.left + width, y1: y, y2: y, class: "chart-grid-line" }));
      text(svg, sar.format(max * ratio), 16, y + 4, { class: "axis-label" });
    });

    rows.forEach((row, index) => {
      const value = Number(row[metric]) || 0;
      const x = margin.left + index * slot + (slot - barWidth) / 2;
      const h = (value / max) * height;
      const y = margin.top + height - h;
      const bar = node("rect", { x, y, width: barWidth, height: h, rx: 6, class: "bar-fill" });
      bar.style.fill = palette[index % palette.length];
      bar.addEventListener("mousemove", (event) => showTip(tip, `${row.branch}: ${sar.format(value)} SAR`, event));
      bar.addEventListener("mouseleave", () => hideTip(tip));
      svg.appendChild(bar);
      text(svg, row.branch, x + barWidth / 2, 322, { class: "axis-label", "text-anchor": "middle" });
    });
  }

  function describeArc(cx, cy, r, start, end) {
    const startX = cx + r * Math.cos(start);
    const startY = cy + r * Math.sin(start);
    const endX = cx + r * Math.cos(end);
    const endY = cy + r * Math.sin(end);
    const large = end - start > Math.PI ? 1 : 0;
    return `M ${cx} ${cy} L ${startX} ${startY} A ${r} ${r} 0 ${large} 1 ${endX} ${endY} Z`;
  }

  function renderCategoryDonut() {
    const svg = document.getElementById("categoryDonutChart");
    const legend = document.getElementById("categoryLegend");
    const tip = document.getElementById("categoryChartTip");
    if (!svg || !legend) return;
    clear(svg);
    legend.innerHTML = "";
    const rows = (data.categories || []).filter((row) => Number(row.exposure) > 0).slice(0, 8);
    if (!rows.length) {
      text(svg, "No category exposure", 122, 160, { class: "chart-empty" });
      return;
    }
    const total = rows.reduce((sum, row) => sum + Number(row.exposure || 0), 0);
    let angle = -Math.PI / 2;
    rows.forEach((row, index) => {
      const value = Number(row.exposure || 0);
      const nextAngle = angle + (value / total) * Math.PI * 2;
      const slice = node("path", { d: describeArc(190, 156, 122, angle, nextAngle), class: "donut-slice" });
      slice.style.fill = palette[index % palette.length];
      slice.addEventListener("mousemove", (event) => showTip(tip, `${row.category}: ${sar.format(value)} SAR`, event));
      slice.addEventListener("mouseleave", () => hideTip(tip));
      svg.appendChild(slice);
      const item = document.createElement("div");
      item.innerHTML = `<span style="background:${palette[index % palette.length]}"></span><strong>${row.category}</strong><em>${sar.format(value)} SAR</em>`;
      legend.appendChild(item);
      angle = nextAngle;
    });
    svg.appendChild(node("circle", { cx: 190, cy: 156, r: 72, class: "donut-hole" }));
    text(svg, "Total", 190, 148, { class: "donut-center", "text-anchor": "middle" });
    text(svg, `${sar.format(total)} SAR`, 190, 172, { class: "donut-value", "text-anchor": "middle" });
  }

  function renderTrend(metric = "exposure") {
    const svg = document.getElementById("trendLineChart");
    if (!svg) return;
    clear(svg);
    const rows = data.trends || [];
    const tip = document.getElementById("trendChartTip");
    if (!rows.length) {
      text(svg, "No history yet", 430, 160, { class: "chart-empty" });
      return;
    }
    const margin = { top: 24, right: 32, bottom: 52, left: 82 };
    const width = 940 - margin.left - margin.right;
    const height = 320 - margin.top - margin.bottom;
    const max = Math.max(...rows.map((row) => Number(row[metric]) || 0), 1);
    const points = rows.map((row, index) => {
      const x = margin.left + (rows.length === 1 ? width / 2 : (index / (rows.length - 1)) * width);
      const y = margin.top + height - ((Number(row[metric]) || 0) / max) * height;
      return { x, y, row };
    });

    [0, 0.5, 1].forEach((ratio) => {
      const y = margin.top + height - height * ratio;
      svg.appendChild(node("line", { x1: margin.left, x2: margin.left + width, y1: y, y2: y, class: "chart-grid-line" }));
      text(svg, sar.format(max * ratio), 16, y + 4, { class: "axis-label" });
    });

    const d = points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
    svg.appendChild(node("path", { d, class: "trend-line" }));
    points.forEach((point, index) => {
      const dot = node("circle", { cx: point.x, cy: point.y, r: 5, class: "trend-dot" });
      dot.addEventListener("mousemove", (event) => showTip(tip, `${point.row.branch} ${point.row.label}: ${sar.format(point.row[metric])}`, event));
      dot.addEventListener("mouseleave", () => hideTip(tip));
      svg.appendChild(dot);
      if (index === 0 || index === points.length - 1 || index % 4 === 0) {
        text(svg, point.row.label, point.x, 304, { class: "axis-label", "text-anchor": "middle" });
      }
    });
  }

  document.querySelectorAll("[data-chart-metric] button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-chart-metric] button").forEach((btn) => btn.classList.remove("active"));
      button.classList.add("active");
      renderBranchBars(button.dataset.metric);
    });
  });

  document.querySelectorAll("[data-trend-metric] button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-trend-metric] button").forEach((btn) => btn.classList.remove("active"));
      button.classList.add("active");
      renderTrend(button.dataset.metric);
    });
  });

  renderBranchBars();
  renderCategoryDonut();
  renderTrend();
})();
