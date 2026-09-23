const { test, expect } = require('@playwright/test');

test('a reader opens OFF even when this browser previously translated the book', async ({ page }) => {
    await page.goto('/library');
    await page.evaluate(() => {
        localStorage.setItem('bt_mode', 'translated');
        localStorage.setItem('bt_book_42_bt_mode', 'bilingual');
    });

    const batches = [];
    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        batches.push(payload);
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/42');
    const bar = page.locator('#bt-bar');
    await expect(bar).toHaveAttribute('data-mode', 'off');
    await page.waitForTimeout(800);
    expect(batches).toHaveLength(0);

    await page.locator('#bt-toggle').click();
    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#paragraph-one .bt-translation')).toHaveText(
        'ES: A quiet production test paragraph.'
    );
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'ES: A second paragraph checks queue order.'
    );
    const afterActivation = batches.length;
    expect(afterActivation).toBeGreaterThan(0);

    await page.evaluate(() => {
        window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });
    await expect(bar).toHaveAttribute('data-mode', 'off');

    await page.reload();
    await expect(bar).toHaveAttribute('data-mode', 'off');
    await page.waitForTimeout(800);
    expect(batches).toHaveLength(afterActivation);

    await page.locator('#bt-toggle').click();
    await expect(bar).toHaveAttribute('data-mode', 'bilingual');
    await page.evaluate(() => {
        history.pushState({}, '', '/read/43');
        window.dispatchEvent(new Event('bt:reader-route'));
    });
    await expect(bar).toHaveAttribute('data-mode', 'off');
});
