import type { EffectDef } from './types';
import { makeSilkGL, makeSilk2d } from './silk';
import { makeAuroraGL } from './aurora';
import { makeFlow2d } from './flowfield';
import { makeAttractor2d } from './attractor';
import { makeTracers2d } from './tracers';
import { makeGrayScottGL, makeGrayScott2d } from './grayscott';
import { makeLife2d } from './life';

/** Dynamic effects by catalog id. Static ids (solid, mesh, grid) are CSS-only and have no entry. */
export const EFFECTS: Record<string, EffectDef> = {
  silk: { scale: 0.35, makeGL: makeSilkGL, make2d: makeSilk2d },
  aurora: { scale: 0.25, makeGL: makeAuroraGL },
  flow: { scale: 0.5, make2d: makeFlow2d },
  attractor: { scale: 0.5, make2d: makeAttractor2d },
  tracers: { scale: 0.5, make2d: makeTracers2d },
  grayscott: { scale: 0.5, makeGL: makeGrayScottGL, make2d: makeGrayScott2d },
  life: { scale: 1, make2d: makeLife2d },
};
