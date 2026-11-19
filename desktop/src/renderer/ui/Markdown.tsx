// The one Markdown component: server strings that are Markdown go through marked (no raw HTML) then DOMPurify, and
// links open externally through main. Every other server string reaches the DOM as text.
import { useMemo, useState } from 'preact/hooks';
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

/** Above this many characters the source is shown as plain text first: parse + sanitise + insert of a page near
 *  serve's 1 MB text cap blocks the window for hundreds of ms. */
export const MD_RENDER_MAX = 300_000;

export function Markdown({ text, class: cls, testid }: { text: string; class?: string; testid?: string }) {
  const [force, setForce] = useState(false);
  const big = !force && String(text ?? '').length > MD_RENDER_MAX;
  const html = useMemo(() => (big ? '' : renderMarkdown(text)), [text, big]);
  if (big) {
    return (
      <div class={`md md--plain ${cls ?? ''}`} data-testid={testid}>
        <div class="notice" data-testid="md-too-big">
          <span class="grow">This page is large ({Math.round(text.length / 1000)} KB), so it is shown as plain text.</span>
          <button type="button" class="btn btn--sm" data-testid="md-render-anyway" onClick={() => setForce(true)}>Render it</button>
        </div>
        <pre class="tk-pre">{text}</pre>
      </div>
    );
  }
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
