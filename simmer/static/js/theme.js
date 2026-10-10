// Workspace appearance only: never sends a simulator appearance command.
const THEME_KEY = 'simmerTheme';
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
let preference = document.documentElement.dataset.themePreference || 'system';

function applyTheme() {
  const resolved = preference === 'system' ? (systemTheme.matches ? 'dark' : 'light') : preference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = preference;
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    'content', resolved === 'dark' ? '#161916' : '#f3f3f0'
  );
  document.querySelectorAll('[data-theme-choice]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.themeChoice === preference));
  });
  window.dispatchEvent(new Event('simmer:themechange'));
}

export function initTheme() {
  document.querySelectorAll('[data-theme-choice]').forEach(button => {
    button.addEventListener('click', () => {
      preference = button.dataset.themeChoice;
      try { localStorage.setItem(THEME_KEY, preference); } catch {}
      applyTheme();
    });
  });
  systemTheme.addEventListener('change', () => {
    if (preference === 'system') applyTheme();
  });
  window.addEventListener('storage', event => {
    if (event.key !== THEME_KEY && event.key !== null) return;
    preference = ['light', 'dark'].includes(event.newValue) ? event.newValue : 'system';
    applyTheme();
  });
  applyTheme();
}

// xterm does its own rendering; read CSS tokens only when its theme changes.
export function terminalTheme() {
  const css = getComputedStyle(document.documentElement);
  const token = name => css.getPropertyValue(name).trim();
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    background: token('--bg-elevated'), foreground: token('--text-primary'),
    cursor: token('--accent'), cursorAccent: token('--bg-elevated'),
    selectionBackground: dark ? '#53624980' : '#bac9b380',
    black: dark ? '#373e34' : '#242923', brightBlack: dark ? '#8f9a84' : '#60675d',
    red: dark ? '#f2938d' : '#b63030', brightRed: dark ? '#ffb1ab' : '#b63030',
    green: dark ? '#8bd3a3' : '#28704a', brightGreen: dark ? '#a8e7bc' : '#28704a',
    yellow: dark ? '#e6bd76' : '#866015', brightYellow: dark ? '#f3d399' : '#866015',
    blue: dark ? '#8dbbf1' : '#2b62ab', brightBlue: dark ? '#b3d4fc' : '#2b62ab',
    magenta: dark ? '#c4a5e8' : '#8250ab', brightMagenta: dark ? '#ddc2fc' : '#8250ab',
    cyan: dark ? '#81cecc' : '#207677', brightCyan: dark ? '#a6e7e5' : '#207677',
    white: dark ? '#b1baa7' : '#60675d', brightWhite: dark ? '#edf0e8' : '#242923',
  };
}
