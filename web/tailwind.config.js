/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#f4f6f8",
          100: "#e8ecf0",
          200: "#c9d2db",
          300: "#9aabbb",
          400: "#6a7f94",
          500: "#4d647a",
          600: "#3b4f61",
          700: "#2f3f4e",
          800: "#283541",
          900: "#222d37",
          950: "#141b22",
        },
        accent: {
          DEFAULT: "#1f6f5b",
          soft: "#d7efe7",
        },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
        display: ['"IBM Plex Sans Condensed"', "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
