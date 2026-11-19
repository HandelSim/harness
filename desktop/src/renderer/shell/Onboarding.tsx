// First run / onboarding (W1): locate the install, create .env, add a key, log in, incompatible version.
import { useState } from 'preact/hooks';
import { backend, keyState } from '../core/app';
import { bridge } from '../core/bridge';
import { backendAction } from './BackendPopover';
import { Button, Field } from '../ui/kit';
import { Icon } from '../ui/Icon';
import { openView } from './dock';
import { onboardingStep } from './onboarding-model';

export function Onboarding() {
  const b = backend.value;
  const [code, setCode] = useState('');
  const step = onboardingStep(b, keyState.value);
  if (!b || step === null) return null;
  return (
    <div class="scrim" style={{ display: 'grid', placeItems: 'center', background: 'var(--scrim)' }}>
      <div class="glass glass--overlay col" style={{ width: 'min(520px, calc(100vw - 32px))', padding: 'var(--sp-6)', gap: 'var(--sp-4)' }}
        data-testid={`onboarding-step-${step}`} role="dialog" aria-label="Set up harness">
        <div class="row"><span class="brand-mark" style={{ width: '22px', height: '22px', borderRadius: '6px' }} />
          <strong style={{ fontSize: 'var(--text-20)' }}>Set up harness</strong><span class="spacer" /><span class="meta">step {step} of 3</span></div>
        {b.phase === 'no-install' ? (
          <>
            <p class="muted">The app needs a harness checkout to run. Pick the folder that contains the <code>harness</code> script.</p>
            {b.problem ? <div class="notice notice--warn">{b.problem.message}</div> : null}
            <div class="row">
              <Button variant="primary" icon="folder" onClick={async () => { const p = await bridge.shell.pickFolder(); if (p) await backendAction({ action: 'set-root', root: p }); }}>Choose folder…</Button>
              <Button variant="ghost" onClick={() => backendAction({ action: 'retry' })}>Search again</Button>
            </div>
          </>
        ) : b.phase === 'needs-config' ? (
          <>
            <p class="muted">The install at <span class="mono small">{b.root}</span> needs configuration before serve can start.</p>
            {b.missing.length ? <div class="notice"><strong>Missing:</strong> {b.missing.join(', ')}</div> : null}
            <div class="row" style={{ flexWrap: 'wrap' }}>
              {b.missing.includes('.env') ? <Button variant="primary" onClick={() => backendAction({ action: 'create-env' })}>Create .env from the example</Button> : null}
              <Button icon="keys" onClick={() => openView('keys')}>Add an API key</Button>
              <Button variant="ghost" icon="retry" onClick={() => backendAction({ action: 'retry' })}>Check again</Button>
            </div>
          </>
        ) : b.phase === 'needs-login' ? (
          <>
            <p class="muted">This serve requires a login code. Run <code>harness serve code</code> in the install, or create one here.</p>
            <Field label="Login code"><input class="input mono" value={code} onInput={(e) => setCode((e.target as HTMLInputElement).value)} autoFocus /></Field>
            <div class="row">
              <Button variant="primary" disabled={!code.trim()} onClick={() => backendAction({ action: 'login', code: code.trim() })}>Log in</Button>
              {b.owned ? <Button onClick={() => backendAction({ action: 'create-code' })}>Create a code</Button> : null}
            </div>
          </>
        ) : (
          <>
            <div class="row"><Icon name="alert" /><strong>Incompatible harness version</strong></div>
            <p class="muted">{b.problem?.message || 'This app needs a newer harness API. Update the harness checkout, then retry.'}</p>
            <div class="row"><Button variant="primary" onClick={() => openView('updates')}>Updates</Button><Button onClick={() => backendAction({ action: 'retry' })}>Retry</Button></div>
          </>
        )}
      </div>
    </div>
  );
}
