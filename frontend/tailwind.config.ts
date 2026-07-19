import type { Config } from "tailwindcss";

// Synthetix Vibe (Neon-Noir / dark) を踏襲 (screen-spec.md §0)。 詳細トークンは Phase 2 で拡張。
const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: "#a855f7", // Electric Purple
        danger: "#ef4444", // Pulse Red
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["Geist Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
