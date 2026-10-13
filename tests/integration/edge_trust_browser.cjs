/* Actual Chromium -> controlled edge -> shipped nginx -> real API/fixtures. */
'use strict';

const { chromium, expect } = require('@playwright/test');
const { spawnSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const image = process.argv[2];
if (!image) throw new Error('usage: node edge_trust_browser.cjs IMAGE');
const container = 'bth-edge-browser-' + randomUUID();
const artifacts = process.env.BT_EDGE_SMOKE_ARTIFACT_DIR
    || fs.mkdtempSync(path.join(os.tmpdir(), 'bth-edge-browser-'));
fs.mkdirSync(artifacts, { recursive: true });

function docker(args) {
    const result = spawnSync('docker', args, { encoding: 'utf8', timeout: 30_000 });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error('docker ' + args[0] + ' failed: ' + result.stderr);
    return result.stdout.trim();
}

async function freePort() {
    const server = net.createServer();
    await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', resolve);
    });
    const port = server.address().port;
    await new Promise(resolve => server.close(resolve));
    return port;
}

async function main() {
    const port = await freePort();
    const origin = 'http://127.0.0.1:' + port;
    let created = false;
    let browser;
    let page;
    try {
        docker([
            'run', '-d', '--name', container, '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m,uid=101,gid=102,mode=700',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--pids-limit', '128', '--memory', '512m',
            '-p', '127.0.0.1:' + port + ':8585',
            '--mount', 'type=bind,src=' + path.resolve(__dirname, '..') + ',dst=/fixture,readonly',
            '-e', 'BT_EDGE_SMOKE_PUBLIC_ORIGIN=' + origin,
            '--entrypoint', 'python', image,
            '/fixture/integration/edge_trust_smoke.py', '--serve-browser',
        ]);
        created = true;
        let ready = false;
        const deadline = Date.now() + 30_000;
        while (Date.now() < deadline) {
            try {
                const response = await fetch(origin + '/bt-api/ping', {
                    signal: AbortSignal.timeout(1_000),
                });
                if (response.status === 200) {
                    ready = true;
                    break;
                }
            } catch (_) { /* Startup polling only; every test assertion fails closed. */ }
            await new Promise(resolve => setTimeout(resolve, 200));
        }
        if (!ready) throw new Error('native browser boundary did not become ready');
        browser = await chromium.launch({
            headless: true,
            ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
                ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
        });
        const context = await browser.newContext({
            userAgent: 'Edge-Trust-Chromium/1.0', serviceWorkers: 'block',
            // The controlled edge replaces this forged identity with its peer.
            extraHTTPHeaders: {
                'X-Forwarded-For': '198.51.100.254', 'X-Real-IP': '198.51.100.254',
                'X-BT-Subject': 'forged-browser-subject',
            },
        });
        page = await context.newPage();
        const errors = [];
        const exchanges = [];
        const translations = [];
        page.on('console', message => {
            if (['error', 'warning'].includes(message.type())) errors.push(message.type());
        });
        page.on('pageerror', () => errors.push('page exception'));
        page.on('requestfailed', () => errors.push('request failed'));
        page.on('response', response => {
            const pathname = new URL(response.url()).pathname;
            if (pathname === '/bt-api/session' && response.request().method() === 'POST') {
                exchanges.push(response.status());
            }
            if (pathname === '/bt-api/translate/batch') translations.push(response.status());
        });

        await page.goto(origin + '/fixture/login?user=3');
        await page.waitForLoadState('networkidle');
        await page.goto(origin + '/read/42');
        await page.waitForLoadState('networkidle');
        const bar = page.locator('#bt-bar');
        await expect(bar).toHaveAttribute('data-mode', 'off');
        expect(exchanges).toEqual([200]);
        expect(translations).toEqual([]);
        const first = (await context.cookies()).find(cookie => cookie.name === 'bt-cwa-session');
        expect(first && first.httpOnly).toBe(true);
        expect(await page.evaluate(() => document.cookie.includes('bt-cwa-session='))).toBe(false);

        await page.locator('#bt-lang').selectOption('Spanish');
        expect(translations).toEqual([]);
        await page.locator('#bt-toggle').click();
        const chapter = page.frameLocator('iframe[title="Book chapter"]');
        await expect(chapter.locator('#native-paragraph .bt-translation')).toHaveText(
            'translated:Native edge browser paragraph 1.'
        );
        await page.evaluate(() => {
            document.querySelector('iframe[title="Book chapter"]').src = '/chapter/2';
        });
        await expect(chapter.locator('#native-paragraph .bt-translation')).toHaveText(
            'translated:Native edge browser paragraph 2.'
        );

        // Delete only the opaque plugin proof; retain native CWA login. Reload
        // must exchange a different HttpOnly proof and leave translation OFF.
        await page.waitForLoadState('networkidle');
        const beforeReload = translations.length;
        await context.clearCookies({ name: 'bt-cwa-session' });
        await page.reload();
        await page.waitForLoadState('networkidle');
        await expect(bar).toHaveAttribute('data-mode', 'off');
        expect(exchanges).toEqual([200, 200]);
        expect(translations.length).toBe(beforeReload);
        const fresh = (await context.cookies()).find(cookie => cookie.name === 'bt-cwa-session');
        expect(fresh && fresh.httpOnly).toBe(true);
        expect(fresh.value).not.toBe(first.value);
        await page.locator('#bt-toggle').click();
        await expect(chapter.locator('#native-paragraph .bt-translation')).toHaveText(
            'translated:Native edge browser paragraph 1.'
        );
        await page.goto(origin + '/read/43');
        await page.waitForLoadState('networkidle');
        await expect(bar).toHaveAttribute('data-mode', 'off');
        expect(exchanges).toEqual([200, 200, 200]);
        expect(translations.length).toBeGreaterThanOrEqual(3);
        expect(translations.every(status => status === 200)).toBe(true);
        expect(errors).toEqual([]);
        await page.screenshot({ path: path.join(artifacts, 'native-edge-reader.png') });
        console.log('PASS: Chromium native login, real exchange/translation, chapter navigation, fresh HttpOnly session and OFF reload');
    } catch (error) {
        if (page) await page.screenshot({ path: path.join(artifacts, 'failure.png') }).catch(() => {});
        if (created) fs.writeFileSync(path.join(artifacts, 'container.log'), docker(['logs', container]));
        throw error;
    } finally {
        if (browser) await browser.close();
        // Only this invocation's disposable test container; no volumes or
        // deployment resources are attached to it.
        if (created) docker(['rm', '-f', container]);
    }
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
