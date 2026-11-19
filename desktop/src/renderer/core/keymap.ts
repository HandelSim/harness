// Chords (PLAN W18): "Mod+K", "Ctrl+Shift+P", "Alt+1", "F6", "Mod+\\". Pure; unit-tested.
export type Platform = 'darwin' | 'linux' | 'win32' | string;
export interface Chord { ctrl: boolean; meta: boolean; alt: boolean; shift: boolean; key: string }
export interface KeyLike { key: string; code?: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean; shiftKey: boolean }

const NAMED: Record<string, string> = { esc: 'escape', return: 'enter', space: ' ', del: 'delete', plus: '=', comma: ',', period: '.', slash: '/', backslash: '\\' };

export function parseChord(text: string, platform: Platform): Chord | null {
  const parts = text.split('+');
  // "Mod++" style: trailing empty part means the key is '+'
  if (parts.length > 1 && parts[parts.length - 1] === '' ) { parts.pop(); parts[parts.length - 1] = '+'; }
  const c: Chord = { ctrl: false, meta: false, alt: false, shift: false, key: '' };
  for (const raw of parts) {
    const p = raw.trim().toLowerCase();
    if (p === 'mod') { if (platform === 'darwin') c.meta = true; else c.ctrl = true; }
    else if (p === 'ctrl' || p === 'control') c.ctrl = true;
    else if (p === 'cmd' || p === 'meta' || p === 'super') c.meta = true;
    else if (p === 'alt' || p === 'option' || p === 'opt') c.alt = true;
    else if (p === 'shift') c.shift = true;
    else if (c.key) return null;
    else c.key = NAMED[p] ?? p;
  }
  return c.key ? c : null;
}

/** Normalised key of an event: letters lower-case, digits from the physical key so Shift/Alt do not change them. */
export function eventKey(e: KeyLike): string {
  const code = e.code || '';
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (code === 'Backslash') return '\\';
  if (code === 'Equal') return '=';
  if (code === 'Minus') return '-';
  if (code === 'Comma') return ',';
  if (code === 'Period') return '.';
  if (code === 'Slash') return '/';
  return e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase();
}

export function chordOf(e: KeyLike): Chord {
  return { ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey, shift: e.shiftKey, key: eventKey(e) };
}

export function chordId(c: Chord): string {
  return `${c.ctrl ? 'c' : ''}${c.meta ? 'm' : ''}${c.alt ? 'a' : ''}${c.shift ? 's' : ''}:${c.key}`;
}

export function formatChord(text: string, platform: Platform): string {
  const c = parseChord(text, platform);
  if (!c) return text;
  const mac = platform === 'darwin';
  const out: string[] = [];
  if (c.ctrl) out.push(mac ? '⌃' : 'Ctrl');
  if (c.alt) out.push(mac ? '⌥' : 'Alt');
  if (c.shift) out.push(mac ? '⇧' : 'Shift');
  if (c.meta) out.push(mac ? '⌘' : 'Super');
  const k = c.key;
  out.push(k === ' ' ? 'Space' : k.length === 1 ? k.toUpperCase() : k[0].toUpperCase() + k.slice(1));
  return mac ? out.join('') : out.join('+');
}

export interface Binding { command: string; chord: string }

export class Keymap {
  private byId = new Map<string, string[]>();
  constructor(private platform: Platform, bindings: Binding[] = []) { this.set(bindings); }

  set(bindings: Binding[]): void {
    this.byId.clear();
    for (const b of bindings) {
      for (const one of b.chord.split(/\s*,\s*/)) {
        const c = parseChord(one, this.platform);
        if (!c) continue;
        const id = chordId(c);
        const list = this.byId.get(id) ?? [];
        if (!list.includes(b.command)) list.push(b.command);
        this.byId.set(id, list);
      }
    }
  }

  /** Commands bound to the event's chord (first wins at dispatch). */
  lookup(e: KeyLike): string[] { return this.byId.get(chordId(chordOf(e))) ?? []; }

  /**
   * W18 terminal rule. While a terminal has focus on Linux/Windows, plain Ctrl+<key> belongs to the PTY;
   * the app is reached with Ctrl+Shift+<same key> (the chord bound to Mod+<key>, or Mod+Shift+<key> when no
   * Mod+<key> binding exists), plus Alt and F-key chords. macOS: Cmd never collides, normal lookup.
   * Returns the commands the app should run, or [] to let xterm handle the key.
   */
  lookupInTerminal(e: KeyLike): string[] {
    if (this.platform === 'darwin') return e.metaKey ? this.lookup(e) : [];
    const k = eventKey(e);
    const fkey = /^f([1-9]|1[0-9]|2[0-4])$/.test(k);
    if (fkey) return this.lookup(e);
    if (e.ctrlKey && e.shiftKey && !e.metaKey) {
      if (k === 'c' || k === 'v') return [];      // terminal copy/paste, handled by the terminal itself
      const stripped = this.byId.get(chordId({ ctrl: true, meta: false, alt: e.altKey, shift: false, key: k }));
      if (stripped?.length) return stripped;
      return this.lookup(e);
    }
    if (e.altKey && !e.ctrlKey && !e.metaKey) return this.lookup(e);
    return [];
  }

  /** Chords bound to more than one command. */
  conflicts(): { chord: string; commands: string[] }[] {
    return [...this.byId.entries()].filter(([, v]) => v.length > 1).map(([chord, commands]) => ({ chord, commands }));
  }
}

/** Defaults merged with user overrides (settings.keymap: command id -> chord; '' unbinds). */
export function mergeBindings(defaults: Binding[], overrides: Record<string, string>): Binding[] {
  const out: Binding[] = [];
  const seen = new Set<string>();
  for (const b of defaults) {
    if (b.command in overrides) {
      if (!seen.has(b.command) && overrides[b.command]) out.push({ command: b.command, chord: overrides[b.command] });
      seen.add(b.command);
    } else out.push(b);
  }
  for (const [command, chord] of Object.entries(overrides)) if (!seen.has(command) && chord) out.push({ command, chord });
  return out;
}

/** True when the event target is a text field (single-letter view shortcuts must not fire there). */
export function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const t = el.tagName;
  return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || el.isContentEditable || !!el.closest?.('.xterm');
}
