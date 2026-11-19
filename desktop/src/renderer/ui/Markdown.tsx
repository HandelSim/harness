// The one Markdown component: server strings that are Markdown go through marked (no raw HTML) then DOMPurify, and
// links open externally through main. Every other server string reaches the DOM as text.
import { useMemo } from 'preact/hooks';
import { Marked } from 'marked';
import DOMPurify from 'dompurify';
import { bridge } from '../core/bridge';

const md = new Marked({ gfm: true, breaks: false, async: false });
// Raw HTML in the source is shown as text, never parsed.
md.use({ renderer: { html({ text }: { text: string }) { return escapeHtml(text); } } });

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string));
}

const ALLOWED_TAGS = ['p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'input', 'span'];
export function renderMarkdown(src: string): string {
  const html = md.parse(String(src ?? '')) as string;
  return DOMPurify.sanitize(html, { ALLOWED_TAGS, ALLOWED_ATTR: ['href', 'title', 'type', 'checked', 'disabled', 'align', 'start'],
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i });
}

export function Markdown({ text, class: cls, testid }: { text: string; class?: string; testid?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return (
    <div class={`md ${cls ?? ''}`} data-testid={testid} dangerouslySetInnerHTML={{ __html: html }}
      onClick={(e) => {
        const a = (e.target as HTMLElement).closest('a');
        if (!a) return;
        e.preventDefault();
        const href = a.getAttribute('href') || '';
        if (/^(https?:|mailto:)/i.test(href)) void bridge.shell.openExternal(href);
      }} />
  );
}
