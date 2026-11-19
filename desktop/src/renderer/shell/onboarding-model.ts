// Onboarding step from backend phase (pure, unit-tested). null = no onboarding.
import type { BackendState, KeyVaultState } from '../../shared/ipc';
export function onboardingStep(b: BackendState | null, _k: KeyVaultState | null): 1 | 2 | 3 | null {
  if (!b) return null;
  switch (b.phase) {
    case 'no-install': return 1;
    case 'needs-config': return 2;
    case 'needs-login': case 'incompatible': return 3;
    default: return null;
  }
}
