const { test, expect } = require('@playwright/test');

function observeBrowserFailures(page, { allowedConsole = [] } = {}) {
    const failures = [];
    page.on('console', message => {
        if (message.type() === 'error' || message.type() === 'warning') {
            const text = message.text();
            if (!allowedConsole.some(pattern => pattern.test(text))) {
                failures.push(`console ${message.type()}: ${text}`);
            }
        }
    });
    page.on('pageerror', error => failures.push(`page error: ${error.message}`));
    page.on('requestfailed', request => {
        failures.push(`request failed: ${request.method()} ${request.url()}`);
    });
    return failures;
}

test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
        localStorage.setItem('bt_mode', 'off');
        localStorage.setItem('bt_prefetch', '0');
        localStorage.setItem('bt_lang', 'Spanish');
    });
});

test('the loader stays inert outside reader routes', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    await page.goto('/library');
    await expect(page.locator('#bt-bar')).toHaveCount(0);
    expect(failures).toEqual([]);
});

test('Kavita activates only on the pinned EPUB route and refreshes one rejected session', async ({ page }) => {
    const failures = observeBrowserFailures(page, {
        allowedConsole: [/^Failed to load resource:.*status of 401\b/],
    });
    await page.addInitScript(() => {
        localStorage.setItem('kavita-user', JSON.stringify({
            token: 'e2e-kavita-access',
            refreshToken: 'must-remain-local'
        }));
    });
    await page.route('**/bt-config.json', route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
            apiUrl: '/bt-api',
            authMode: 'reader_session',
            credentials: 'same-origin',
            readerType: 'kavita',
            readerVersion: '0.9.0.2',
            readerContractVersion: 'kavita-0.9.0.2-epub-v1',
        }),
    }));
    const exchanges = [];
    await page.route('**/bt-api/session', async route => {
        exchanges.push({
            method: route.request().method(),
            headers: route.request().headers(),
            body: route.request().postData(),
        });
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            headers: {
                'Cache-Control': 'no-store',
                'Set-Cookie': 'bt-session=e2eopaque012345678901234567890123; Path=/; HttpOnly; SameSite=Strict',
            },
            body: JSON.stringify({
                status: 'ok', expires_in: 300,
                reader_type: 'kavita', reader_version: '0.9.0.2',
            }),
        });
    });
    const payloads = [];
    let batchAttempts = 0;
    await page.route('**/bt-api/translate/batch', async route => {
        batchAttempts++;
        if (batchAttempts === 1) {
            await route.fulfill({
                status: 401,
                contentType: 'application/json',
                body: JSON.stringify({ error: 'unauthorized' }),
            });
            return;
        }
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/library/7/series/42/book/99');
    await expect(page.getByRole('toolbar', { name: /book translator/i })).toBeVisible();
    await page.locator('#bt-toggle').click();
    await expect.poll(() => exchanges.length).toBe(2);
    await expect.poll(() => payloads.length).toBe(1);

    expect(exchanges[0].method).toBe('POST');
    expect(exchanges[0].body).toBeNull();
    expect(exchanges[0].headers.authorization).toBe('Bearer e2e-kavita-access');
    expect(JSON.stringify(exchanges)).not.toContain('must-remain-local');
    expect(payloads[0].book_id).toBe('7:42');
    expect(payloads[0].chapter_id).toBe('99');
    await expect(page.locator('#kavita-paragraph .bt-translation')).toHaveText(
        'ES: A Kavita EPUB paragraph.'
    );

    const attemptsBeforeManga = batchAttempts;
    await page.evaluate(() => {
        history.pushState({}, '', '/library/7/series/42/manga/99');
        document.querySelector('.book-content').innerHTML =
            '<p>Manga content must not be translated.</p>';
    });
    await expect(page.locator('#bt-bar')).toBeHidden();
    await page.waitForTimeout(500);
    expect(batchAttempts).toBe(attemptsBeforeManga);
    expect(failures).toEqual([]);
});

test('Kavita manga routes never load the overlay', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    await page.route('**/bt-config.json', route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
            apiUrl: '/bt-api', authMode: 'reader_session',
            credentials: 'same-origin', readerType: 'kavita',
            readerVersion: '0.9.0.2',
            readerContractVersion: 'kavita-0.9.0.2-epub-v1',
        }),
    }));
    let exchangeCount = 0;
    await page.route('**/bt-api/session', route => {
        exchangeCount++;
        return route.abort();
    });

    await page.goto('/library/7/series/42/manga/99');
    await page.waitForTimeout(100);
    await expect(page.locator('#bt-bar')).toHaveCount(0);
    expect(exchangeCount).toBe(0);
    expect(failures).toEqual([]);
});

test('the real overlay translates, reports state, and keeps cloud consent explicit', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    const payloads = [];

    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(
                    text => `${payload.source_lang}->${payload.target_lang}: ${text}`
                ),
                backends: payload.paragraphs.map(() => 'e2e'),
                cached: payload.paragraphs.map(() => false),
            }),
        });
    });

    await page.goto('/read/42');

    const toolbar = page.getByRole('toolbar', { name: /book translator/i });
    await expect(toolbar).toBeVisible();
    // The live region is intentionally visually hidden while the plugin is idle;
    // role locators exclude hidden elements by default, so inspect its contract
    // directly until translation work makes it visible.
    await expect(page.locator('#bt-status')).toHaveAttribute('role', 'status');
    await expect(page.locator('#bt-status')).toHaveAttribute('aria-live', 'polite');
    await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0');

    await page.locator('#bt-toggle').click();
    await expect.poll(() => payloads.length).toBeGreaterThan(0);
    await expect(page.getByRole('status')).toBeVisible();
    expect(payloads[0].allow_cloud_fallback).toBe(false);
    expect(payloads[0].book_id).toBe('42');

    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#paragraph-one .bt-translation')).toHaveText(
        'English->Spanish: A quiet production test paragraph.'
    );
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'English->Spanish: A second paragraph checks queue order.'
    );

    const settings = page.getByRole('button', { name: /settings|ajustes/i });
    await expect(settings).toHaveAttribute('aria-expanded', 'false');
    await settings.click();
    await expect(settings).toHaveAttribute('aria-expanded', 'true');

    const cloudConsent = page.getByRole('switch', { name: /cloud|nube/i });
    await expect(cloudConsent).toHaveAttribute('aria-checked', 'false');
    await cloudConsent.click();
    await expect(cloudConsent).toHaveAttribute('aria-checked', 'true');

    await page.locator('#bt-lang').selectOption('French');
    await expect.poll(() => payloads.some(payload => (
        payload.source_lang === 'English'
        && payload.target_lang === 'French'
        && payload.allow_cloud_fallback === true
    ))).toBe(true);
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'English->French: A second paragraph checks queue order.'
    );

    await page.locator('#bt-source-lang').selectOption('Spanish');
    await expect.poll(() => payloads.some(payload => (
        payload.source_lang === 'Spanish'
        && payload.target_lang === 'French'
        && payload.allow_cloud_fallback === true
    ))).toBe(true);
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'Spanish->French: A second paragraph checks queue order.'
    );

    const feedbackPosts = [];
    await page.route('**/bt-api/feedback', async route => {
        feedbackPosts.push(route.request().postDataJSON());
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ feedback: { para_key: 'e2e', rating: 1 } }),
        });
    });
    await chapter.locator('#paragraph-one .bt-feedback .bt-fb-btn').first().click();
    await expect.poll(() => feedbackPosts.length).toBe(1);
    expect(feedbackPosts[0].rating).toBe(1);
    expect(typeof feedbackPosts[0].para_key).toBe('string');
    expect(feedbackPosts[0].book_id).toBe('42');

    const snapshot = await toolbar.ariaSnapshot();
    expect(snapshot).toContain('button');
    expect(snapshot).toContain('combobox');
    const screenshot = await page.screenshot({ animations: 'disabled' });
    expect(screenshot.byteLength).toBeGreaterThan(1000);
    expect(failures).toEqual([]);
});

test('attaching the reader observer does not cancel or duplicate active translation work', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    const payloads = [];

    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        // Keep the first request active beyond the legacy 350 ms observer poll.
        // Late observer attachment used to abort this admitted request and send
        // the same paragraph again, wasting provider quota.
        if (payloads.length === 1) await new Promise(resolve => setTimeout(resolve, 500));
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/42');
    await page.locator('#bt-toggle').click();

    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'ES: A second paragraph checks queue order.'
    );
    expect(payloads.flatMap(payload => payload.paragraphs)).toEqual([
        'A quiet production test paragraph.',
        'A second paragraph checks queue order.',
    ]);
    expect(failures).toEqual([]);
});

test('route re-entry attaches its observer before starting translation work', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    const payloads = [];

    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        if (payloads.length === 1) await new Promise(resolve => setTimeout(resolve, 500));
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/42');
    await page.evaluate(() => {
        history.pushState({}, '', '/library');
        window.dispatchEvent(new Event('bt:reader-route'));
    });
    await expect(page.locator('#bt-bar')).toBeHidden();
    await page.evaluate(() => document.querySelector('#bt-toggle').click());
    await page.evaluate(() => {
        history.pushState({}, '', '/read/42');
        window.dispatchEvent(new Event('bt:reader-route'));
    });

    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'ES: A second paragraph checks queue order.'
    );
    expect(payloads.flatMap(payload => payload.paragraphs)).toEqual([
        'A quiet production test paragraph.',
        'A second paragraph checks queue order.',
    ]);
    expect(failures).toEqual([]);
});

test('polling fallback attaches a delayed iframe without replaying admitted work', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    const payloads = [];

    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        if (payloads.length === 1) await new Promise(resolve => setTimeout(resolve, 500));
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/delayed');
    await page.locator('#bt-toggle').click();

    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#paragraph-two .bt-translation')).toHaveText(
        'ES: A second paragraph checks queue order.'
    );
    expect(payloads.flatMap(payload => payload.paragraphs)).toEqual([
        'A quiet production test paragraph.',
        'A second paragraph checks queue order.',
    ]);
    expect(failures).toEqual([]);
});

test('replacing an observed iframe attaches the new document before translation', async ({ page }) => {
    const failures = observeBrowserFailures(page);
    const payloads = [];

    await page.route('**/bt-api/translate/batch', async route => {
        const payload = route.request().postDataJSON();
        payloads.push(payload);
        if (payloads.length === 1) await new Promise(resolve => setTimeout(resolve, 500));
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/42');
    // Ensure the original document has been observed, then replace it and
    // populate the new same-origin document in one task. The mutation callback
    // must attach before the next browser action starts translation.
    await page.waitForTimeout(400);
    await page.evaluate(() => {
        const replacement = document.createElement('iframe');
        replacement.title = 'Book chapter';
        document.querySelector('iframe[title="Book chapter"]').replaceWith(replacement);
        const doc = replacement.contentDocument;
        doc.open();
        doc.write(`<!doctype html><html><body><main class="chapter">
          <p id="replacement-one">A replacement chapter paragraph.</p>
          <p id="replacement-two">A second replacement paragraph.</p>
        </main></body></html>`);
        doc.close();
    });
    await page.locator('#bt-toggle').click();

    const chapter = page.frameLocator('iframe[title="Book chapter"]');
    await expect(chapter.locator('#replacement-two .bt-translation')).toHaveText(
        'ES: A second replacement paragraph.'
    );
    expect(payloads.flatMap(payload => payload.paragraphs)).toEqual([
        'A replacement chapter paragraph.',
        'A second replacement paragraph.',
    ]);
    expect(failures).toEqual([]);
});

test('forwarded auth presents the SSO cookie to the identity edge without a token', async ({ page, context }) => {
    const failures = observeBrowserFailures(page);
    await context.addCookies([{
        name: 'authentik_session',
        value: 'browser-edge-proof',
        domain: '127.0.0.1',
        path: '/',
        httpOnly: true,
        sameSite: 'Lax',
    }]);
    await page.route('**/bt-config.json', route => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
            apiUrl: '/bt-api', authMode: 'forwarded', credentials: 'include',
        }),
    }));
    let requestHeaders = null;
    await page.route('**/bt-api/translate/batch', async route => {
        requestHeaders = route.request().headers();
        const payload = route.request().postDataJSON();
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                translations: payload.paragraphs.map(text => `ES: ${text}`),
            }),
        });
    });

    await page.goto('/read/42');
    await page.locator('#bt-toggle').click();
    await expect.poll(() => requestHeaders).not.toBeNull();

    expect(requestHeaders.cookie).toContain('authentik_session=browser-edge-proof');
    expect(requestHeaders['x-bt-token']).toBeUndefined();
    expect(failures).toEqual([]);
});

test('rate limiting is presented as a visible non-fatal wait state', async ({ page }) => {
    // Chromium reports an expected HTTP 429 as a console resource error even
    // though fetch receives and handles it. Only that exact scenario is allowed.
    const failures = observeBrowserFailures(page, {
        allowedConsole: [/^Failed to load resource:.*status of 429\b/],
    });
    await page.route('**/bt-api/translate/batch', route => route.fulfill({
        status: 429,
        contentType: 'application/json',
        headers: { 'Retry-After': '1' },
        body: JSON.stringify({
            error: 'rate_limited',
            retry_after: 1,
            retry_safe: true,
            scope: 'api_admission',
        }),
    }));

    await page.goto('/read/42');
    await page.locator('#bt-toggle').click();

    await expect(page.locator('#bt-bar')).toHaveAttribute('data-state', 'ratelimit');
    await expect(page.getByRole('status')).toBeVisible();
    await expect(page.getByRole('status')).toContainText(/waiting|esperando/i);
    expect(failures).toEqual([]);
});
