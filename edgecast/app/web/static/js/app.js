// Edgecast UI helpers: charts (uPlot), copy-to-clipboard, small toasts. Keep tiny.
(function () {
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  // Categorical slots validated against the dark surface (#18181b): blue, orange, aqua.
  const SERIES = { PAPER: "#3987e5", LIVE: "#d95926", MANUAL: "#199e70" };

  function axes(yFmt) {
    const stroke = "#a1a1aa", grid = { stroke: "#27272a", width: 1 };
    return [
      { stroke, grid, ticks: { show: false }, font: "11px Inter" },
      { stroke, grid, ticks: { show: false }, font: "11px Inter", size: 56, values: (u, v) => v.map(yFmt) },
    ];
  }

  // series: {name: [[unixSec, value], ...]} -> aligned uPlot data
  function align(series) {
    const names = Object.keys(series);
    const ts = [...new Set(names.flatMap((n) => series[n].map((p) => p[0])))].sort((a, b) => a - b);
    const idx = new Map(ts.map((t, i) => [t, i]));
    const cols = names.map((n) => {
      const col = new Array(ts.length).fill(null);
      series[n].forEach(([t, v]) => (col[idx.get(t)] = v));
      return col;
    });
    return { names, data: [ts, ...cols] };
  }

  window.ecLine = function (el, series, opts = {}) {
    if (!window.uPlot || !el) return;
    const { names, data } = align(series);
    if (!data[0].length) { el.innerHTML = '<p class="py-10 text-center text-sm text-zinc-500">No data yet.</p>'; return; }
    const fmt = opts.fmt || ((v) => (v == null ? "—" : "$" + v.toFixed(2)));
    const make = () => new uPlot({
      width: el.clientWidth, height: opts.height || 220,
      cursor: { points: { size: 8 }, drag: { x: false, y: false } },
      legend: { show: true, live: true },
      scales: { x: { time: true } },
      axes: axes(fmt),
      series: [{ label: "Time" }, ...names.map((n) => ({
        label: n, stroke: opts.color || SERIES[n] || "#3987e5", width: 2, value: (u, v) => fmt(v),
        fill: opts.fill ? (opts.fill) : undefined, spanGaps: true, points: { show: false },
      }))],
    }, data, el);
    let plot = make();
    new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height: opts.height || 220 })).observe(el);
  };

  // Reliability diagram: predicted vs observed with the diagonal as reference.
  window.ecReliability = function (el, bins) {
    if (!window.uPlot || !el) return;
    if (!bins.length) { el.innerHTML = '<p class="py-10 text-center text-sm text-zinc-500">No resolved predictions yet.</p>'; return; }
    const xs = bins.map((b) => b.mean_p), ys = bins.map((b) => b.freq);
    const pct = (v) => (v == null ? "—" : (100 * v).toFixed(0) + "%");
    const plot = new uPlot({
      width: el.clientWidth, height: 260, legend: { show: true },
      scales: { x: { time: false, range: [0, 1] }, y: { range: [0, 1] } },
      axes: [{ stroke: "#a1a1aa", grid: { stroke: "#27272a" }, values: (u, v) => v.map(pct) },
             { stroke: "#a1a1aa", grid: { stroke: "#27272a" }, values: (u, v) => v.map(pct), size: 48 }],
      series: [{ label: "Predicted", value: (u, v) => pct(v) },
               { label: "Observed", stroke: "#3987e5", width: 2, value: (u, v) => pct(v), points: { size: 9, fill: "#3987e5" } },
               { label: "Perfect", stroke: "#52525b", width: 1, dash: [4, 4], points: { show: false } }],
    }, [xs, ys, xs], el);
    new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height: 260 })).observe(el);
  };

  window.ecCopy = async function (text, btn) {
    try { await navigator.clipboard.writeText(text); } catch (e) { return; }
    if (btn) { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = t), 1200); }
  };

  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.target.id === "toast") setTimeout(() => (e.target.innerHTML = ""), 6000);
  });
})();
