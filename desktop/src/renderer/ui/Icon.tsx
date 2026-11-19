// Minimal stroke icon set (24-grid, 1.75 stroke), drawn inline so no asset requests are made.
const P: Record<string, string> = {
  home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z',
  queue: 'M4 6h16M4 12h16M4 18h10',
  sessions: 'M4 5h16v11H8l-4 4z',
  findings: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5',
  vault: 'M4 4h16v16H4zM9 4v16M13 9h4M13 13h4',
  jira: 'M12 3 3 12l9 9 9-9zM12 8l-4 4 4 4 4-4z',
  debug: 'M8 8h8v9a4 4 0 0 1-8 0zM9 4l1.5 2M15 4l-1.5 2M4 12h4M16 12h4M5 18l3-1M19 18l-3-1',
  keys: 'M14 10a4 4 0 1 0-3.5 3.97L10 14v2H8v2H6v2H3v-3l6.03-6.03A4 4 0 0 0 14 10zM15.5 8.5h.01',
  updates: 'M12 3v12M7 10l5 5 5-5M5 21h14',
  settings: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  plus: 'M12 5v14M5 12h14',
  x: 'M6 6l12 12M18 6 6 18',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.3-4.3',
  terminal: 'M4 5h16v14H4zM8 10l2.5 2L8 14M13 14h3',
  play: 'M7 5v14l11-7z',
  stop: 'M7 7h10v10H7z',
  pause: 'M8 5v14M16 5v14',
  chevronDown: 'M6 9l6 6 6-6',
  chevronRight: 'M9 6l6 6-6 6',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  alert: 'M12 4 2.5 20h19zM12 10v4M12 17h.01',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5M12 8h.01',
  globe: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.6 3.5 5.6 3.5 9s-1 6.4-3.5 9c-2.5-2.6-3.5-5.6-3.5-9s1-6.4 3.5-9z',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  refresh: 'M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4',
  back: 'M15 6l-6 6 6 6',
  forward: 'M9 6l6 6-6 6',
  split: 'M4 4h16v16H4zM12 4v16',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  send: 'M4 12 20 4l-6 16-2.5-6.5z',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01',
  server: 'M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01',
  folder: 'M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z',
  chat: 'M4 5h16v11H9l-5 4z',
  file: 'M6 3h8l5 5v13H6zM14 3v5h5',
  grip: 'M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01',
  sidebar: 'M4 4h16v16H4zM9 4v16',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z',
  archive: 'M3 5h18v4H3zM5 9v10h14V9M10 13h4',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
  retry: 'M4 4v6h6M4.6 15a8 8 0 1 0 1.9-8.3L4 10',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  dot: 'M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z',
  layers: 'M12 3 2 8l10 5 10-5zM2 13l10 5 10-5',
};
export function Icon({ name, size = 16, class: cls }: { name: keyof typeof P | string; size?: number; class?: string }) {
  const d = P[name] ?? P.dot;
  return (
    <svg class={`icon ${cls ?? ''}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}
