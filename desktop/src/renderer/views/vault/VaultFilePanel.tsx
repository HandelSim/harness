// Vault file panel ('vault-file' kind): one vault file from /vault/file. Markdown renders through ui/Markdown with its
// in-vault links rewritten to open here (another vault-file tab); images load as bytes into a blob URL (the page's CSP
// allows blob: images, not the server's URL); text shows up to the server's cap with a notice when cut.
import { useEffect, useMemo, useState } from 'preact/hooks';
import type { VaultOverview } from '../../../shared/api';
import { useTopic } from '../../core/app';
import { api, bridge } from '../../core/bridge';
import { openWeb, type PanelCtx } from '../../shell/dock';
import { Button, EmptyState, ErrorPanel, Skeleton } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { Markdown } from '../../ui/Markdown';
import { bytesPath, linkify, parseVaultHref, type PageRow } from './model';
import { openVaultFile } from './VaultView';

interface VaultFile { path: string; kind: 'image' | 'markdown' | 'text'; text?: string; truncated?: boolean; url?: string }

export default function VaultFilePanel({ ctx }: { ctx: PanelCtx }) {
  const rel = String(ctx.params.value.path ?? '');
  const [f, setF] = useState<VaultFile | null>(null);
  const [err, setErr] = useState<{ code: string; message: string } | null>(null);
  const [n, setN] = useState(0);
  const vault = useTopic('vault');
  const pages = ((vault?.data.value as VaultOverview | undefined)?.pages ?? []) as PageRow[];
  useEffect(() => { ctx.setTitle(rel.slice(rel.lastIndexOf('/') + 1) || 'Vault file'); }, [rel]);
  useEffect(() => {
    let live = true;
    setF(null); setErr(null);
    void bridge.api.get<VaultFile>(api('/vault/file'), { path: rel }).then((r) => {
      if (!live) return;
      if (r.ok && r.data) setF(r.data); else if (!r.ok) setErr(r.error);
    });
    return () => { live = false; };
  }, [rel, n]);
  const md = useMemo(() => (f?.kind === 'markdown' ? linkify(f.text ?? '', rel, pages) : ''), [f, rel, pages]);
  const onClick = (e: MouseEvent) => {
    const a = (e.target as HTMLElement).closest('a');
    const to = parseVaultHref(a?.getAttribute('href'));
    if (!to) return;
    e.preventDefault();
    openVaultFile(to);
  };
  return (
    <div class="view vfile" data-testid="vault-file">
      <div class="view-head">
        <Icon name={f?.kind === 'image' ? 'image' : 'file'} size={15} />
        <span class="mono ellipsis grow" title={rel} data-testid="vault-file-path">{rel}</span>
        <Button size="sm" variant="ghost" icon="refresh" iconOnly aria-label="Reload" title="Reload" onClick={() => setN(n + 1)} />
        <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open the web version" title="Open the web version"
          onClick={() => openWeb(`/vault?path=${encodeURIComponent(rel)}`, { title: `${rel} (web)` })} />
      </div>
      <div class="view-body vfile-body" onClick={onClick}>
        {!rel ? <EmptyState icon="file" text="No file chosen." />
          : err ? <ErrorPanel error={err} onRetry={() => setN(n + 1)} />
            : !f ? <Skeleton rows={10} />
              : (
                <>
                  {f.truncated ? <div class="notice notice--warn" data-testid="vault-file-cut"><Icon name="alert" size={16} /><div>This file is long; only its start is shown.</div></div> : null}
                  {f.kind === 'image' ? <VaultImage rel={f.path || rel} />
                    : f.kind === 'markdown' ? (md.trim() ? <Markdown text={md} class="vfile-md" testid="vault-file-md" /> : <EmptyState icon="file" text="This page is empty." />)
                      : (f.text ? <pre class="pre vfile-text" data-testid="vault-file-text">{f.text}</pre> : <EmptyState icon="file" text="This file is empty." />)}
                </>
              )}
      </div>
    </div>
  );
}

function VaultImage({ rel }: { rel: string }) {
  const [src, setSrc] = useState('');
  const [err, setErr] = useState('');
  useEffect(() => {
    let url = '';
    let live = true;
    void bridge.api.bytes(bytesPath(rel)).then((r) => {
      if (!live) return;
      if (!r.ok) { setErr('The image could not be loaded.'); return; }
      url = URL.createObjectURL(new Blob([r.data as BlobPart], { type: r.mime || 'application/octet-stream' }));
      setSrc(url);
    });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [rel]);
  if (err) return <div class="inline-err" role="alert">{err}</div>;
  if (!src) return <Skeleton rows={4} />;
  return <img class="vfile-img" src={src} alt={rel} data-testid="vault-file-img" />;
}
