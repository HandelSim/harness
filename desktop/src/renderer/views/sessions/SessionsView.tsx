// Sessions view (3.6): every agent session the server knows, working ones first; a click opens its transcript.
import { useEffect, useState } from 'preact/hooks';
import type { SessionRow } from '../../../shared/api';
import { useTopic } from '../../core/app';
import { ago, basename, cost, tokens } from '../../core/format';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { hooks } from '../../shell/commands';
import { VIEW_WEB, WEB } from '../../shell/panels';
import { AsOf, Button, EmptyState, TopicBody } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { sortSessions } from '../transcript/model';

const open = (r: SessionRow) => openPanel('transcript', r.id, { title: r.title || r.id.slice(0, 12), params: { sid: r.id } });

export default function SessionsView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('sessions');
  const [q, setQ] = useState('');
  useEffect(() => { ctx.setTitle('Sessions'); }, []);
  return (
    <div class="view sessions-view" data-testid="view-sessions">
      <div class="view-head">
        <Button size="sm" variant="primary" icon="plus" onClick={() => hooks.newSession?.(false)}>New session</Button>
        <label class="search-field"><Icon name="search" size={13} />
          <input class="input input--bare" placeholder="Filter sessions" aria-label="Filter sessions" value={q} onInput={(ev) => setQ((ev.target as HTMLInputElement).value)} />
        </label>
        <span class="grow" />
        <AsOf e={e} />
        <Button size="sm" icon="globe" iconOnly title="Open the web version" onClick={() => openWeb(VIEW_WEB.sessions, { title: 'Sessions (web)' })} />
      </div>
      <div class="view-body slist">
        <TopicBody e={e} webPath={VIEW_WEB.sessions} openWeb={(p) => openWeb(p)}>
          {(rows: SessionRow[]) => {
            const list = sortSessions(rows ?? [], q);
            if (!list.length) return <EmptyState icon="sessions" text={q ? 'No session matches the filter.' : 'No sessions yet.'} />;
            return list.map((r) => (
              <div key={r.id} class="list-row srow" role="button" tabIndex={0} data-testid={`session-row-${r.id}`}
                onClick={() => open(r)} onKeyDown={(ev) => { if (ev.key === 'Enter') open(r); }}>
                <span class={`dot ${r.working ? 'dot--live' : ''}`} aria-label={r.working ? 'working' : 'idle'} />
                <span class="srow-title ellipsis">{r.title || r.id}</span>
                <span class="meta mono ellipsis srow-dir" title={r.directory}>{basename(r.directory)}</span>
                <span class="meta ellipsis">{r.agent}{r.source === 'host' ? ' · host' : ''}</span>
                <span class="meta tnum srow-num">{tokens((r.tokens_in || 0) + (r.tokens_out || 0))}{r.cost ? ` · ${cost(r.cost)}` : ''}</span>
                <span class="meta tnum srow-num">{ago(r.updated)}</span>
              </div>
            ));
          }}
        </TopicBody>
      </div>
    </div>
  );
}
