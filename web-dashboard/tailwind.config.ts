import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Space Grotesk", "IBM Plex Sans", "Segoe UI", "sans-serif"],
        mono: ["IBM Plex Mono", "Consolas", "monospace"]
      },
      colors: {
        arena: {
          bg: "#0A1014",
          panel: "#101A22",
          panel2: "#142330",
          border: "#203746",
          muted: "#88A2B6",
          text: "#E8F3FA",
          bid: "#2AD38B",
          ask: "#FF5A72",
          warn: "#F8C35A",
          accent: "#4FB0FF"
        }
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(79,176,255,0.2), 0 10px 30px rgba(0,0,0,0.35)"
      }
    }
  },
  plugins: []
};

export default config;
