import { test, expect, type Page } from '@playwright/test';

async function search(page: Page, query: string) {
  await page.getByRole('searchbox').fill(query);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
}

test('search, edit the queue, and control the Python player', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Connected to Python player')).toBeVisible();
  await search(page, 'satellites');
  const results = page.getByRole('list', { name: 'Search results' });
  const queue = page.getByRole('list', { name: 'Playback queue' });
  await expect(results.getByRole('listitem')).toHaveCount(3);
  await page.getByRole('button', { name: 'Play Blue Hour', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Blue Hour');
  await page.getByRole('button', { name: 'Queue Night Drive', exact: true }).click();
  await expect(queue.getByRole('listitem')).toHaveCount(2);
  await page.getByRole('button', { name: 'Play next Morning Tide', exact: true }).click();
  await expect(queue.getByRole('listitem').nth(1)).toContainText('Morning Tide');
  await page.screenshot({ path: 'test-results/panel-desktop.png', fullPage: true });
  await page.getByRole('button', { name: 'Move up Night Drive' }).click();
  await expect(queue.getByRole('listitem').nth(1)).toContainText('Night Drive');
  await page.getByRole('button', { name: 'Move down Night Drive' }).click();
  await expect(queue.getByRole('listitem').nth(2)).toContainText('Night Drive');
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Pause', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '+10 s', exact: true }).click();
  await expect(page.getByText('0:10', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '−10 s', exact: true }).click();
  await expect(page.getByText('0:00', { exact: true })).toBeVisible();
  await page.getByRole('slider', { name: 'Volume' }).focus();
  await page.getByRole('slider', { name: 'Volume' }).press('ArrowLeft');
  await expect(page.getByText('65%', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Morning Tide');
  await page.getByRole('button', { name: 'Previous', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Blue Hour');
  await page.getByRole('button', { name: 'Jump to Night Drive' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Night Drive');
  await expect(queue.getByRole('listitem').nth(2)).toHaveAttribute('aria-current', 'true');
  await page.getByRole('button', { name: 'Remove Blue Hour' }).click();
  await expect(queue.getByRole('listitem')).toHaveCount(2);
  await page.getByRole('button', { name: 'Clear upcoming' }).click();
  await expect(queue.getByRole('listitem')).toHaveCount(1);
  await expect(queue).toContainText('Night Drive');
  // Duplicate media IDs still produce independently editable queue rows.
  await page.getByRole('button', { name: 'Queue Night Drive', exact: true }).click();
  await expect(queue.getByRole('listitem')).toHaveCount(2);
  await page.getByRole('button', { name: 'Remove Night Drive' }).nth(1).click();
  await expect(queue.getByRole('listitem')).toHaveCount(1);
  await page.getByRole('button', { name: 'Remove Night Drive' }).click();
  await expect(queue.getByRole('listitem')).toHaveCount(0);
  await expect(page.getByText('Player is idle')).toBeVisible();
});

test('empty search, provider failure, reconnect and narrow layout', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await search(page, 'empty');
  await expect(page.getByText('No songs found. Try another title or artist.')).toBeVisible();
  await search(page, 'offline');
  await expect(page.getByRole('alert')).toContainText('Fixture catalogue is offline');
  await search(page, 'satellites');
  await expect(page.getByRole('list', { name: 'Search results' }).getByRole('listitem')).toHaveCount(3);
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/panel-mobile.png', fullPage: true });
  await page.route('**/api/status', route => route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'Player disconnected' }) }));
  await expect(page.getByRole('alert')).toContainText('Player disconnected');
  await expect(page.getByRole('button', { name: 'Play Blue Hour', exact: true })).toBeDisabled();
  await page.unroute('**/api/status');
  await page.getByRole('button', { name: 'Reconnect' }).click();
  await expect(page.getByText('Connected to Python player')).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
