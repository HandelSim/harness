// Engine run state policy (pure, so it is unit-tested): hidden -> pause; on battery with background.onBattery
// 'static' -> pause (the last frame stays, nothing animates); unfocused -> background.onBlur; else run.
import type { BgRunState } from '../../../shared/bg';

export function bgRunState(w: { visible: boolean; focused: boolean }, onBlur: BgRunState, battery: boolean, onBattery: 'run' | 'static'): BgRunState {
  if (!w.visible || (battery && onBattery === 'static')) return 'pause';
  return w.focused ? 'run' : onBlur;
}
