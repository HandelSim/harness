import { defineConfig } from '@playwright/test';

// Electron e2e (PLAN 8). Run through scripts/e2e.mjs, which builds first and wraps in xvfb-run when DISPLAY is unset.
export default defineConfig({
  testDir: 'e2e',
  testMatch: /.*\.spec\.ts$/,
  timeout: 60000,
  expect: { timeout: 10000 },
  workers: Number(process.env.HARNESS_DESKTOP_E2E_WORKERS || 1),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never', outputFolder: 'test-results/html' }]] : [['list']],
  outputDir: 'test-results/artifacts',
  use: { trace: process.env.CI ? 'retain-on-failure' : 'off', screenshot: 'only-on-failure' },
});
