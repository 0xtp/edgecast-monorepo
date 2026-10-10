/** Built with the standalone Tailwind CLI (no Node runtime in production). */
module.exports = {
  content: ["./app/web/templates/**/*.html", "./app/web/static/js/app.js", "./app/web/**/*.py"],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"] },
      colors: {
        // Deep navy primary, matched to the STOEX reference UI
        brand: {
          50: "#eef0fb", 100: "#dfe2f7", 200: "#c3c8ef", 300: "#9aa2e2", 400: "#6d76d0",
          500: "#4a51bd", 600: "#3238a3", 700: "#262a8a", 800: "#1d2075", 900: "#16186a", 950: "#0e0f47",
        },
      },
      boxShadow: { card: "0 1px 2px 0 rgb(16 24 40 / 0.04)", pop: "0 12px 32px -8px rgb(16 24 40 / 0.18)" },
    },
  },
  plugins: [],
};
