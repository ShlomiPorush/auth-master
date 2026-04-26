/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["../static/public/**/*.{html,js}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      colors: {
        navy: {
          950: "#060a13",
          900: "#0b1120",
          800: "#111827",
          700: "#1a2332",
          600: "#243044",
          500: "#334155",
        },
        accent: {
          DEFAULT: "#3b82f6",
          glow: "rgba(59,130,246,0.15)",
        },
      },
    },
  },
  plugins: [],
};
