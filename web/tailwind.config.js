/** @type {import('tailwindcss').Config} */
export default {
  // `dark:` follows the app theme (ThemeContext toggles <html class="dark">),
  // not the OS; with the default "media" a dark app on a light OS rendered
  // light-surface fields with the dark theme's white text.
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // Semantic theme tokens (web/src/index.css). Views should prefer these
      // over raw gray-*/blue-* utilities so the Dark / Light / System themes
      // in index.css drive every surface. `extend` merges with the default
      // palette, so existing gray/blue/red classes keep working unchanged.
      colors: {
        app: 'var(--bg)',
        elevated: 'var(--bg-elevated)',
        hover: 'var(--bg-hover)',
        surface: 'var(--surface)',
        line: 'var(--border)',
        primary: 'var(--text-primary)',
        secondary: 'var(--text-secondary)',
        muted: 'var(--text-muted)',
        accent: {
          DEFAULT: 'var(--accent)',
          hover: 'var(--accent-hover)',
          soft: 'var(--accent-soft)',
          contrast: 'var(--accent-contrast)',
        },
        success: 'var(--success)',
        warning: 'var(--warning)',
        danger: 'var(--danger)',
      },
    },
  },
  plugins: [],
}
