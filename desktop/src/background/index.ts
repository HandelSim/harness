// Public entry of the background engine (contract: src/shared/bg.ts, PLAN 5.4).
export { createBackground } from './engine';
export { DEFAULT_BG, CATALOG, normalizeConfig } from './catalog';
// Non-contract additions (W2-G): no-GPU policy. The engine object also has setLowPower(mode) and lowPower().
export { probeGpu, lowPowerPlan, type GpuInfo, type LowPowerMode, type LowPowerPlan } from './gpu';
