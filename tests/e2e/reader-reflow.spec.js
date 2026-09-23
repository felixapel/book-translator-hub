const { test, expect } = require('@playwright/test');

// Real layout and native scroll events, with the documented rendition event
// boundary simulated at 100 ms. The full bundled EPUB engine is additionally
// exercised by the manual compatibility probe recorded with the change.
async function readingPosition(page) {
    return page.evaluate(() => {
        const iframe = document.querySelector('iframe');
        const clip = document.querySelector('.epub-container');
        const frameRect = iframe.getBoundingClientRect();
        const clipRect = clip.getBoundingClientRect();
        const visible = Array.from(iframe.contentDocument.querySelectorAll('p'))
            .filter(el => Array.from(el.getClientRects()).some(rect =>
                rect.bottom + frameRect.top > clipRect.top
                && rect.top + frameRect.top < clipRect.bottom));
        return { first: visible[0]?.id, top: clip.scrollTop };
    });
}

async function openReflowReader(page, requests, aborted) {
    await page.addInitScript(() => {
        localStorage.setItem('bt_prefetch', '0');
        localStorage.setItem('bt_lang', 'Spanish');
    });
    page.on('requestfailed', request => {
        if (request.url().endsWith('/translate/batch')) aborted.push(request.postDataJSON());
    });
    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        requests.push(payload.paragraphs);
        // Keep the provider response in flight when the delayed relocation
        // caused by restoring our own reading anchor arrives.
        await new Promise(resolve => setTimeout(resolve, 350));
        await route.fulfill({
            status: 200, contentType: 'application/json',
            body: JSON.stringify({ translations: payload.paragraphs.map(text => `ES: ${text}`) }),
        });
    });
    await page.goto('/read/reflow');
    await expect(page.locator('#bt-bar')).toHaveAttribute('data-mode', 'off');
    await page.locator('#bt-toggle').click();
    await expect(page.frameLocator('iframe').locator('#source-0 .bt-translation')).toHaveCount(1);
    await page.locator('#next-page').click();
    await expect(page.frameLocator('iframe').locator('#source-1 .bt-translation')).toHaveCount(1);
}

test('owned reflow relocation keeps the paragraph and never replays provider work', async ({ page }) => {
    const requests = [], aborted = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await openReflowReader(page, requests, aborted);
    const before = await readingPosition(page);
    await page.locator('#bt-toggle').click();
    await expect(page.locator('#bt-bar')).toHaveAttribute('data-mode', 'translated');
    await page.waitForTimeout(1200);
    expect((await readingPosition(page)).first).toBe(before.first);
    const paragraphs = requests.flat();
    expect(new Set(paragraphs).size).toBe(paragraphs.length);
    expect(aborted).toEqual([]);
    await page.locator('#bt-toggle').click();
    await expect(page.locator('#bt-bar')).toHaveAttribute('data-mode', 'off');
    const countAtOff = requests.length;
    await page.waitForTimeout(400);
    expect((await readingPosition(page)).first).toBe(before.first);
    expect(requests).toHaveLength(countAtOff);
    expect(errors).toEqual([]);
});

test('user navigation overrides a pending owned reflow relocation', async ({ page }) => {
    const requests = [], aborted = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await openReflowReader(page, requests, aborted);
    const before = await readingPosition(page);
    const destinationParagraph = page.frameLocator('iframe').locator('#source-8');
    const destinationText = (await destinationParagraph.textContent()).trim();
    expect(requests.flat()).not.toContain(destinationText);
    await page.locator('#bt-toggle').click();
    await page.locator('#jump-page').click();
    const destination = await readingPosition(page);
    expect(destination.first).not.toBe(before.first);
    await expect(destinationParagraph).toHaveText(`ES: ${destinationText}`);
    await page.waitForTimeout(1200);
    expect((await readingPosition(page)).first).toBe(destination.first);
    expect(requests.flat().filter(text => text === destinationText)).toHaveLength(1);
    expect(errors).toEqual([]);
});
