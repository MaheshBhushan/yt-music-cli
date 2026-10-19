import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

// Use the checkout venv locally; CI installs the project into its Python.
const venv = resolve('../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.YTM_TEST_PYTHON || (existsSync(venv) ? venv : 'python');

export default defineConfig({
  testDir: './tests',
  workers: 1,
  timeout: 15000,
  globalTimeout: 25000,
  use: { baseURL: 'http://127.0.0.1:8766', headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: `"${python}" -u -m tests.panel_fixture --port 8766`,
    cwd: '..',
    url: 'http://127.0.0.1:8766/api/status',
    timeout: 10000,
    reuseExistingServer: false,
    stdout: 'pipe',
  },
});
