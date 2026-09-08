const fs = require('fs');
const assert = require('assert');
const path = require('path');
const jsdom = require("jsdom");
const { JSDOM } = jsdom;

const root = path.resolve(__dirname, '../..');
const code = fs.readFileSync(path.join(root, 'static/translator.js'), 'utf-8');
const loaderCode = fs.readFileSync(path.join(root, 'static/loader.js'), 'utf-8');

assert(!/getItem\(['"]bt_token['"]\)/.test(loaderCode),
    'The proxy loader must never recover a JavaScript-readable API token from localStorage');
assert(/fetch\(['"]\/bt-config\.json['"]/.test(loaderCode)
        && /cache:\s*['"]no-store['"]/.test(loaderCode),
    'The proxy loader must fetch its non-cacheable server-owned auth contract');
assert(/authMode:\s*managed\.authMode/.test(loaderCode)
        && /credentials:\s*managed\.credentials/.test(loaderCode)
        && /apiToken:\s*''/.test(loaderCode)
        && !/authMode:\s*existing\.authMode/.test(loaderCode),
    'Managed authentication settings must not be overridden by page JavaScript');
assert(!/\.refreshToken\b/.test(loaderCode),
    'The Kavita bootstrap must never access the native refresh token');
assert(/AUTH_MODE\s*===\s*'token'\s*&&\s*cfg\.apiToken/.test(code),
    'The browser must send X-BT-Token only in explicit token mode');
assert(/AUTH_MODE\s*===\s*'forwarded'[\s\S]{0,80}?['"]include['"]/.test(code),
    'Forwarded mode must present the browser cookie to the identity edge');
assert(/id="bt-source-lang"/.test(code)
        && /localStorage\.setItem\(['"]bt_source_lang['"]/.test(code)
        && /source_lang:\s*SOURCE_LANG/.test(code),
    'The reader must expose and persist a bounded source-language selector');
assert(/const FIRST_VISIBLE_CHUNK = 1;/.test(code)
        && /const VISIBLE_CHUNK = boundedInteger\(cfg\.batchSize, 1, 50, 5\);/.test(code)
        && /const PREFETCH_CHUNK = VISIBLE_CHUNK;/.test(code)
        && /const PREFETCH_GAP_MS = boundedInteger\(cfg\.prefetchGapMs, 0, 10000, 0\);/.test(code),
    'The queue must optimize first-paint latency while bounding later batches');
assert(!/BT_CLIENT_MIN_REQUEST_GAP_MS/.test(code),
    'Successful requests must not pay an unconditional client-side delay');
assert(/paragraphTextCache = new WeakMap\(\)/.test(code)
        && /paragraphTextCache\.get\(el\)/.test(code),
    'Paragraph extraction must be cached within one DOM generation');
assert(/status\.onkeydown/.test(code)
        && /event\.key === 'Enter' \|\| event\.key === ' '/.test(code)
        && /status\.setAttribute\('tabindex', '0'\)/.test(code),
    'The visible retry control must be keyboard operable');
assert(/window\.requestAnimationFrame\(\(\) => target\.focus\(\)\)/.test(code)
        && /gear\.focus\(\)/.test(code),
    'The settings dialog must move and restore keyboard focus');

let fetchCalls = [];
let fetchResponses = [];
let activeFetches = 0;
let maxActiveFetches = 0;

global.fetch = async (url, options) => {
    if (String(url).endsWith('/provider-policy')) {
        return {
            ok: true,
            status: 200,
            json: async () => ({ primary: 'local', fallback: 'remote', generation: '0123456789abcdef0123456789abcdef' }),
            headers: { get: () => null }
        };
    }
    activeFetches++;
    if (activeFetches > maxActiveFetches) {
        maxActiveFetches = activeFetches;
    }
    fetchCalls.push({url, options, time: Date.now()});
    
    // Simulate network delay
    await new Promise(r => setTimeout(r, 50));
    
    const nextResp = fetchResponses.shift();
    activeFetches--;
    
    if (nextResp instanceof Error) throw nextResp;
    if (typeof nextResp === 'function') return nextResp();
    
    return {
        ok: nextResp ? nextResp.status < 400 : false,
        status: nextResp ? nextResp.status : 500,
        json: async () => nextResp.body,
        headers: { get: (k) => nextResp.headers?.[k] }
    };
};

const dom = new JSDOM(`
<!DOCTYPE html>
<html>
<body>
    <div id="viewer">
        <iframe></iframe>
    </div>
</body>
</html>
`, {
    url: "http://localhost/",
    runScripts: "dangerously"
});

const iframeDoc = dom.window.document.querySelector("iframe").contentDocument;
// The paragraphs live inside a <section class="chapter"> wrapper — the shape
// Calibre-converted epubs actually ship. The wrapper matches the
// [class*="chapter"] candidate selector; a regression here means the whole
// chapter gets translated as ONE mega-block (seen in production, v2.1.1).
// The fetch-order assertions below double as the regression test: with the
// wrapper bug, the first fetched "paragraph" would be the concatenated text.
iframeDoc.body.innerHTML = `
  <section class="chapter">
    <p class="calibre1">visible 1</p>
    <p class="calibre1">visible 2</p>
    <p class="calibre1">prefetch 1</p>
    <p class="calibre1">prefetch 2</p>
    <p class="calibre1">prefetch 3</p>
    <p class="calibre1">prefetch 4</p>
  </section>
`;

iframeDoc.querySelectorAll('p').forEach((p, idx) => {
    p.getBoundingClientRect = () => ({
        width: 100, height: 20,
        left: 0, top: idx < 2 ? 0 : 1000 // First two are visible
    });
});
iframeDoc.querySelector('section').getBoundingClientRect = () => ({
    width: 100, height: 2000, left: 0, top: 0 // wrapper is "visible" too — worst case
});
dom.window.innerWidth = 800;
dom.window.innerHeight = 600;
dom.window.localStorage.setItem('bt_mode', 'translated');
dom.window.localStorage.setItem('bt_prefetch', '1');
dom.window.localStorage.setItem('bt_lang', 'English');

dom.window.requestAnimationFrame = (cb) => setTimeout(cb, 16);
dom.window.fetch = global.fetch;

const scriptEl = dom.window.document.createElement("script");
scriptEl.textContent = code;
dom.window.document.body.appendChild(scriptEl);

async function wait(ms) {
    return new Promise(r => setTimeout(r, ms));
}

async function captureAuthTransport(config, enableCloudFallback = false) {
    const authDom = new JSDOM(`
<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>
`, { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' });
    authDom.window.BOOK_TRANSLATOR = Object.assign({ apiUrl: '/bt-api' }, config);
    authDom.window.localStorage.setItem(
        'bt_mode', enableCloudFallback ? 'off' : 'translated');
    authDom.window.localStorage.setItem('bt_prefetch', '0');
    authDom.window.localStorage.setItem('bt_lang', 'Spanish');
    authDom.window.requestAnimationFrame = (cb) => setTimeout(cb, 0);

    const authDoc = authDom.window.document.querySelector('iframe').contentDocument;
    authDoc.body.innerHTML = '<p>credential transport probe</p>';
    authDoc.querySelector('p').getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });

    let captured = null;
    authDom.window.fetch = async (url, options) => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true,
                status: 200,
                json: async () => ({ primary: 'local', fallback: 'remote', generation: '0123456789abcdef0123456789abcdef' }),
                headers: { get: () => null }
            };
        }
        captured = captured || { url, options };
        const count = JSON.parse(options.body).paragraphs.length;
        return {
            ok: true,
            status: 200,
            json: async () => ({ translations: Array(count).fill('translated') }),
            headers: { get: () => null }
        };
    };

    const authScript = authDom.window.document.createElement('script');
    authScript.textContent = code;
    authDom.window.document.body.appendChild(authScript);
    if (enableCloudFallback) {
        const controlDeadline = Date.now() + 2000;
        while (
            !authDom.window.document.querySelector('[data-action="cloud-fallback"]')
            && Date.now() < controlDeadline
        ) await wait(20);
        const toggle = authDom.window.document.querySelector(
            '[data-action="cloud-fallback"]');
        assert(toggle, 'Cloud fallback must have an explicit reader control');
        toggle.click();
        authDom.window.document.getElementById('bt-toggle').click();
    }
    const deadline = Date.now() + 2000;
    while (!captured && Date.now() < deadline) await wait(20);
    assert(captured, `Expected a request for auth mode ${config.authMode}`);
    // Let the successful response finish rendering before closing jsdom;
    // otherwise an intentionally detached Window can race the queue's final
    // cache paint and obscure the transport assertion below.
    await wait(30);
    authDom.window.close();
    return captured.options;
}

async function assertProviderPolicyControls() {
    const cases = [
        {
            policy: { primary: 'remote', fallback: 'local', generation: '0123456789abcdef0123456789abcdef' },
            control: false,
            markers: ['cloud-active']
        },
        {
            policy: { primary: 'local', fallback: 'remote', generation: '0123456789abcdef0123456789abcdef' },
            control: true,
            markers: ['remote-fallback']
        },
        {
            policy: { primary: 'remote', fallback: 'remote', generation: '0123456789abcdef0123456789abcdef' },
            control: true,
            markers: ['cloud-active', 'remote-secondary']
        },
        {
            policy: { primary: 'local', fallback: 'local', generation: '0123456789abcdef0123456789abcdef' },
            control: false,
            markers: []
        },
        {
            policy: { primary: 'local', fallback: null, generation: '0123456789abcdef0123456789abcdef' },
            control: false,
            markers: []
        }
    ];
    for (const scenario of cases) {
        const policyDom = new JSDOM(
            '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
            { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
        );
        policyDom.window.BOOK_TRANSLATOR = { apiUrl: '/bt-api', authMode: 'cwa_session' };
        policyDom.window.localStorage.setItem('bt_mode', 'off');
        policyDom.window.fetch = async (url) => {
            assert(String(url).endsWith('/provider-policy'));
            return {
                ok: true,
                status: 200,
                json: async () => scenario.policy,
                headers: { get: () => null }
            };
        };
        const policyScript = policyDom.window.document.createElement('script');
        policyScript.textContent = code;
        policyDom.window.document.body.appendChild(policyScript);
        const deadline = Date.now() + 2000;
        while (!policyDom.window.document.querySelector('#bt-menu')
                && Date.now() < deadline) await wait(10);
        await wait(20);
        const control = policyDom.window.document.querySelector(
            '[data-action="cloud-fallback"]');
        assert.strictEqual(Boolean(control), scenario.control,
            `Unexpected remote fallback control for ${JSON.stringify(scenario.policy)}`);
        const markers = Array.from(policyDom.window.document.querySelectorAll(
            '[data-provider-policy]')).map(element => element.dataset.providerPolicy);
        assert.deepStrictEqual(markers, scenario.markers,
            `Unexpected privacy marker for ${JSON.stringify(scenario.policy)}`);
        policyDom.window.close();
    }
}

async function assertStalePolicyIsRefetchedWithoutTranslationReplay() {
    const staleDom = new JSDOM(
        '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
        { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
    );
    staleDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api',
        authMode: 'cwa_session',
    };
    staleDom.window.localStorage.setItem('bt_mode', 'translated');
    staleDom.window.localStorage.setItem('bt_prefetch', '0');
    staleDom.window.localStorage.setItem('bt_lang', 'Spanish');
    staleDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const paragraph = staleDom.window.document.querySelector('iframe')
        .contentDocument.body.appendChild(
            staleDom.window.document.querySelector('iframe')
                .contentDocument.createElement('p')
        );
    paragraph.textContent = 'policy generation probe';
    paragraph.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });

    let policyCalls = 0;
    let translationCalls = 0;
    let submittedPolicy = null;
    staleDom.window.fetch = async (url, options) => {
        if (String(url).endsWith('/provider-policy')) {
            policyCalls++;
            return {
                ok: true,
                status: 200,
                json: async () => policyCalls === 1
                    ? { primary: 'local', fallback: null, generation: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }
                    : { primary: 'remote', fallback: 'local', generation: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' },
                headers: { get: () => null },
            };
        }
        translationCalls++;
        submittedPolicy = JSON.parse(options.body).provider_policy;
        return {
            ok: false,
            status: 409,
            json: async () => ({ error: 'provider_policy_changed' }),
            headers: { get: () => null },
        };
    };

    const script = staleDom.window.document.createElement('script');
    script.textContent = code;
    staleDom.window.document.body.appendChild(script);
    const deadline = Date.now() + 2000;
    while ((policyCalls < 2 || translationCalls < 1)
            && Date.now() < deadline) await wait(20);
    await wait(40);

    assert.strictEqual(translationCalls, 1,
        'A policy-generation 409 must not replay book text automatically');
    assert.deepStrictEqual(submittedPolicy, {
        primary: 'local',
        fallback: null,
        generation: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    });
    assert(staleDom.window.document.querySelector(
        '[data-provider-policy="cloud-active"]'),
    'A stale reader must refetch and render the new remote-primary warning');
    staleDom.window.close();
}

async function assertAmbiguous429IsTerminal() {
    const retryDom = new JSDOM(
        '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
        { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
    );
    retryDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api', authMode: 'cwa_session'
    };
    retryDom.window.localStorage.setItem('bt_mode', 'translated');
    retryDom.window.localStorage.setItem('bt_prefetch', '0');
    retryDom.window.localStorage.setItem('bt_lang', 'Spanish');
    retryDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const paragraph = retryDom.window.document.querySelector('iframe')
        .contentDocument.body.appendChild(
            retryDom.window.document.querySelector('iframe')
                .contentDocument.createElement('p')
        );
    paragraph.textContent = 'ambiguous 429 probe';
    paragraph.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });

    let translationCalls = 0;
    retryDom.window.fetch = async (url) => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true, status: 200,
                json: async () => ({
                    primary: 'local', fallback: null,
                    generation: '0123456789abcdef0123456789abcdef'
                }),
                headers: { get: () => null },
            };
        }
        translationCalls++;
        return {
            ok: false,
            status: 429,
            json: async () => ({
                error: 'rate_limited', retry_after: 1,
                scope: 'unknown'
            }),
            headers: { get: () => '1' },
        };
    };

    const script = retryDom.window.document.createElement('script');
    script.textContent = code;
    retryDom.window.document.body.appendChild(script);
    await wait(1300);

    assert.strictEqual(translationCalls, 1,
        'A 429 without retry_safe must never replay possibly-admitted book text');
    assert.strictEqual(
        retryDom.window.document.getElementById('bt-bar').dataset.state,
        'error',
        'An ambiguous 429 must become an actionable terminal failure'
    );
    retryDom.window.close();
}

async function assertConfiguredBatchSizeIsUsed() {
    const batchDom = new JSDOM(
        '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
        { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
    );
    batchDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api', authMode: 'cwa_session', batchSize: 3
    };
    batchDom.window.localStorage.setItem('bt_mode', 'translated');
    batchDom.window.localStorage.setItem('bt_prefetch', '0');
    batchDom.window.localStorage.setItem('bt_lang', 'Spanish');
    batchDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const document = batchDom.window.document.querySelector('iframe').contentDocument;
    document.body.innerHTML = '<p>one</p><p>two</p><p>three</p><p>four</p><p>five</p>';
    document.querySelectorAll('p').forEach(paragraph => {
        paragraph.getBoundingClientRect = () => ({
            width: 100, height: 20, left: 0, top: 0
        });
    });
    const batchLengths = [];
    batchDom.window.fetch = async (url, options) => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true, status: 200,
                json: async () => ({
                    primary: 'local', fallback: null,
                    generation: '0123456789abcdef0123456789abcdef'
                }),
                headers: { get: () => null },
            };
        }
        const payload = JSON.parse(options.body);
        batchLengths.push(payload.paragraphs.length);
        return {
            ok: true, status: 200,
            json: async () => ({
                translations: payload.paragraphs.map(text => `ES: ${text}`)
            }),
            headers: { get: () => null },
        };
    };
    const script = batchDom.window.document.createElement('script');
    script.textContent = code;
    batchDom.window.document.body.appendChild(script);
    const deadline = Date.now() + 2000;
    while (batchLengths.reduce((sum, count) => sum + count, 0) < 5
            && Date.now() < deadline) await wait(20);

    assert.deepStrictEqual(batchLengths, [1, 3, 1],
        'Managed batchSize must preserve first paint and enlarge later batches');
    batchDom.window.close();
}

async function assertSafe429RetryBoundSurvivesRediscovery() {
    const retryDom = new JSDOM(
        '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
        { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
    );
    retryDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api', authMode: 'cwa_session'
    };
    retryDom.window.localStorage.setItem('bt_mode', 'translated');
    retryDom.window.localStorage.setItem('bt_prefetch', '0');
    retryDom.window.localStorage.setItem('bt_lang', 'Spanish');
    retryDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const document = retryDom.window.document.querySelector('iframe').contentDocument;
    document.body.innerHTML = '<p>stable rate-limit paragraph</p>';
    document.querySelector('p').getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });
    let translationCalls = 0;
    retryDom.window.fetch = async url => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true, status: 200,
                json: async () => ({
                    primary: 'local', fallback: null,
                    generation: '0123456789abcdef0123456789abcdef'
                }),
                headers: { get: () => null },
            };
        }
        translationCalls++;
        return {
            ok: false, status: 429,
            json: async () => ({
                error: 'rate_limited', retry_after: 1,
                retry_safe: true, scope: 'api_admission'
            }),
            headers: { get: () => '1' },
        };
    };
    const script = retryDom.window.document.createElement('script');
    script.textContent = code;
    retryDom.window.document.body.appendChild(script);

    // Rediscover the same paragraph during each admission wait. Queue objects
    // are transient; the retry budget must remain stable for this generation.
    [300, 1300, 2300].forEach(delay => setTimeout(() => {
        const marker = document.createElement('div');
        document.body.appendChild(marker);
    }, delay));
    await wait(3400);

    assert.strictEqual(translationCalls, 3,
        'Rediscovery must not reset the three-response safe 429 bound');
    assert.strictEqual(
        retryDom.window.document.getElementById('bt-bar').dataset.state,
        'error',
        'Exhausting safe admission retries must become an actionable failure'
    );
    retryDom.window.close();
}

async function assertVisibleWorkInterruptsPrefetchDelay() {
    const pacingDom = new JSDOM(
        '<!DOCTYPE html><html><body><div id="viewer"><iframe></iframe></div></body></html>',
        { url: 'http://reader.example.test/read/1', runScripts: 'dangerously' }
    );
    pacingDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api', authMode: 'cwa_session',
        batchSize: 1, prefetchGapMs: 1000
    };
    pacingDom.window.localStorage.setItem('bt_mode', 'translated');
    pacingDom.window.localStorage.setItem('bt_prefetch', '1');
    pacingDom.window.localStorage.setItem('bt_lang', 'Spanish');
    pacingDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const document = pacingDom.window.document.querySelector('iframe').contentDocument;
    document.body.innerHTML = '<p id="first">first visible</p><p id="background">background</p>';
    document.getElementById('first').getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });
    document.getElementById('background').getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 1000
    });
    let renderedHook = null;
    pacingDom.window.reader = {
        rendition: { on: (name, callback) => {
            if (name === 'rendered') renderedHook = callback;
        } }
    };
    const calls = [];
    pacingDom.window.fetch = async (url, options) => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true, status: 200,
                json: async () => ({
                    primary: 'local', fallback: null,
                    generation: '0123456789abcdef0123456789abcdef'
                }),
                headers: { get: () => null },
            };
        }
        const payload = JSON.parse(options.body);
        calls.push({ time: Date.now(), paragraphs: payload.paragraphs });
        return {
            ok: true, status: 200,
            json: async () => ({
                translations: payload.paragraphs.map(text => `ES: ${text}`)
            }),
            headers: { get: () => null },
        };
    };
    const script = pacingDom.window.document.createElement('script');
    script.textContent = code;
    pacingDom.window.document.body.appendChild(script);
    let deadline = Date.now() + 1000;
    while ((calls.length < 1 || !renderedHook) && Date.now() < deadline) {
        await wait(10);
    }
    assert(renderedHook, 'The EPUB rendered hook must be attached');
    await wait(30); // ensure the sole pump is waiting on the background gap

    const paragraph = document.createElement('p');
    paragraph.textContent = 'new visible work';
    paragraph.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });
    document.body.appendChild(paragraph);
    const admittedAt = Date.now();
    renderedHook();
    deadline = Date.now() + 500;
    while (!calls.some(call => call.paragraphs.includes('new visible work'))
            && Date.now() < deadline) await wait(5);
    const visibleCall = calls.find(call => call.paragraphs.includes('new visible work'));

    assert(visibleCall, 'New visible work must preempt a paced background queue');
    assert(visibleCall.time - admittedAt < 150,
        'Visible work must not inherit any part of the 1000ms prefetch delay');
    pacingDom.window.close();
}

async function assertManagedLoaderContract() {
    const loaderDom = new JSDOM(
        '<!DOCTYPE html><html><head></head><body></body></html>',
        { url: 'https://books.example.test/read/1', runScripts: 'dangerously' }
    );
    let request = null;
    loaderDom.window.BOOK_TRANSLATOR = {
        authMode: 'token', apiToken: 'page-spoof', targetLang: 'Spanish'
    };
    loaderDom.window.fetch = async (url, options) => {
        request = { url, options };
        return {
            ok: true,
            json: async () => ({
                apiUrl: '/bt-api', authMode: 'forwarded', credentials: 'include',
                batchSize: 10, prefetchGapMs: 1000
            })
        };
    };
    const element = loaderDom.window.document.createElement('script');
    element.textContent = loaderCode;
    loaderDom.window.document.head.appendChild(element);
    const deadline = Date.now() + 1000;
    while (!loaderDom.window.document.querySelector('script[src*="translator.js"]')
            && Date.now() < deadline) await wait(10);

    assert(request, 'Loader must request the managed browser configuration');
    assert.strictEqual(request.url, '/bt-config.json');
    assert.strictEqual(request.options.credentials, 'same-origin');
    assert.strictEqual(request.options.cache, 'no-store');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.authMode, 'forwarded');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.credentials, 'include');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.apiToken, '');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.batchSize, 10);
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.prefetchGapMs, 1000);
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.targetLang, 'Spanish');
    assert(loaderDom.window.document.querySelector('link[href*="translator.css"]'));
    assert(loaderDom.window.document.querySelector('script[src*="translator.js"]'));
    loaderDom.window.close();
}

async function assertKavitaSessionLoaderContract() {
    const loaderDom = new JSDOM(
        '<!DOCTYPE html><html><head></head><body><main>Library</main></body></html>',
        {
            url: 'https://kavita.example.test/library',
            runScripts: 'dangerously'
        }
    );
    loaderDom.window.localStorage.setItem('kavita-user', JSON.stringify({
        token: 'native-access-token',
        refreshToken: 'must-never-be-forwarded'
    }));
    const requests = [];
    loaderDom.window.fetch = async (url, options) => {
        requests.push({ url, options });
        if (url === '/bt-config.json') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    apiUrl: '/bt-api',
                    authMode: 'reader_session',
                    credentials: 'same-origin',
                    readerType: 'kavita',
                    readerVersion: '0.9.0.2',
                    readerContractVersion: 'kavita-0.9.0.2-epub-v1'
                })
            };
        }
        assert.strictEqual(url, '/bt-api/session');
        return {
            ok: true,
            status: 200,
            json: async () => ({
                status: 'ok', expires_in: 300,
                reader_type: 'kavita', reader_version: '0.9.0.2'
            })
        };
    };

    const element = loaderDom.window.document.createElement('script');
    element.textContent = loaderCode;
    loaderDom.window.document.head.appendChild(element);
    await wait(30);
    assert.strictEqual(
        loaderDom.window.document.querySelectorAll('script[src*="translator.js"]').length,
        0,
        'Kavita assets must stay inert outside the exact EPUB reader route'
    );

    loaderDom.window.history.pushState(
        {}, '', '/library/7/series/42/book/99'
    );
    const deadline = Date.now() + 1000;
    while (!loaderDom.window.document.querySelector('script[src*="translator.js"]')
            && Date.now() < deadline) await wait(10);

    assert.strictEqual(requests.length, 2,
        'SPA activation must perform one config fetch and one session exchange');
    const exchange = requests[1];
    assert.strictEqual(exchange.options.method, 'POST');
    assert.strictEqual(exchange.options.credentials, 'same-origin');
    assert.strictEqual(exchange.options.cache, 'no-store');
    assert.strictEqual(exchange.options.redirect, 'error');
    assert.strictEqual(exchange.options.headers.Authorization,
        'Bearer native-access-token');
    assert.strictEqual(exchange.options.body, undefined,
        'The credential exchange must have an empty body');
    assert(!JSON.stringify(exchange).includes('must-never-be-forwarded'),
        'The Kavita refresh token must never leave localStorage');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.readerType, 'kavita');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.readerVersion, '0.9.0.2');
    assert.strictEqual(
        loaderDom.window.BOOK_TRANSLATOR.readerContractVersion,
        'kavita-0.9.0.2-epub-v1'
    );
    assert.strictEqual(typeof loaderDom.window.__BT_REFRESH_SESSION, 'function');
    loaderDom.window.close();
}


async function assertKavitaDirectLoaderContract() {
    const loaderDom = new JSDOM(
        '<!DOCTYPE html><html><head></head><body><main>Library</main></body></html>',
        {
            url: 'https://kavita.example.test/library',
            runScripts: 'dangerously'
        }
    );
    const requests = [];
    loaderDom.window.fetch = async (url, options) => {
        requests.push({ url, options });
        if (url === '/bt-config.json') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    apiUrl: '/bt-api',
                    authMode: 'cwa_session',
                    credentials: 'same-origin',
                    readerType: 'kavita',
                    readerVersion: '0.9.1.4',
                    readerContractVersion: 'kavita-0.9.0.2-epub-v1',
                    batchSize: 6,
                    prefetchGapMs: 0
                })
            };
        }
        throw new Error('unexpected fetch: ' + url);
    };

    const element = loaderDom.window.document.createElement('script');
    element.textContent = loaderCode;
    loaderDom.window.document.head.appendChild(element);
    await wait(30);
    assert.strictEqual(
        loaderDom.window.document.querySelectorAll('script[src*="translator.js"]').length,
        0,
        'Kavita assets must stay inert outside the exact EPUB reader route'
    );

    loaderDom.window.history.pushState(
        {}, '', '/library/7/series/42/book/99'
    );
    const deadline = Date.now() + 1000;
    while (!loaderDom.window.document.querySelector('script[src*="translator.js"]')
            && Date.now() < deadline) await wait(10);

    assert(loaderDom.window.document.querySelector('script[src*="translator.js"]'),
        'Kavita translator assets must load upon entering the reader route without requiring session exchange');
    assert.strictEqual(requests.length, 1, 'Direct mode only fetches bt-config.json');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.readerType, 'kavita');
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.readerVersion, '0.9.1.4');
    assert.strictEqual(
        loaderDom.window.BOOK_TRANSLATOR.readerContractVersion,
        'kavita-0.9.0.2-epub-v1'
    );
    assert.strictEqual(loaderDom.window.BOOK_TRANSLATOR.batchSize, 6);
    loaderDom.window.close();
}

async function assertKavitaReaderAdapterContract() {
    const kavitaDom = new JSDOM(`<!DOCTYPE html><html><body>
      <main class="book-container">
        <div class="book-content"><p id="kavita-one">Kavita paragraph one.</p></div>
      </main>
    </body></html>`, {
        url: 'https://kavita.example.test/library/7/series/42/book/99',
        runScripts: 'dangerously'
    });
    kavitaDom.window.BOOK_TRANSLATOR = {
        apiUrl: '/bt-api',
        authMode: 'reader_session',
        credentials: 'same-origin',
        readerType: 'kavita',
        readerVersion: '0.9.0.2',
        readerContractVersion: 'kavita-0.9.0.2-epub-v1'
    };
    kavitaDom.window.localStorage.setItem('bt_mode', 'bilingual');
    kavitaDom.window.localStorage.setItem('bt_prefetch', '0');
    kavitaDom.window.localStorage.setItem('bt_lang', 'Spanish');
    kavitaDom.window.requestAnimationFrame = cb => setTimeout(cb, 0);
    const payloads = [];
    kavitaDom.window.fetch = async (url, options) => {
        if (String(url).endsWith('/provider-policy')) {
            return {
                ok: true,
                status: 200,
                json: async () => ({ primary: 'local', fallback: null, generation: '0123456789abcdef0123456789abcdef' }),
                headers: { get: () => null }
            };
        }
        const payload = JSON.parse(options.body);
        payloads.push(payload);
        return {
            ok: true,
            status: 200,
            json: async () => ({
                translations: payload.paragraphs.map(text => `ES: ${text}`)
            }),
            headers: { get: () => null }
        };
    };
    const first = kavitaDom.window.document.getElementById('kavita-one');
    first.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });

    const script = kavitaDom.window.document.createElement('script');
    script.textContent = code;
    kavitaDom.window.document.body.appendChild(script);
    let deadline = Date.now() + 2000;
    while (payloads.length === 0 && Date.now() < deadline) await wait(20);

    assert.strictEqual(payloads[0].book_id, '7:42');
    assert.strictEqual(payloads[0].chapter_id, '99');
    assert.strictEqual(payloads[0].paragraphs[0], 'Kavita paragraph one.');
    assert.strictEqual(
        first.querySelector('.bt-translation').textContent,
        'ES: Kavita paragraph one.'
    );

    const root = kavitaDom.window.document.querySelector('.book-content');
    root.innerHTML = '<p id="kavita-two">Kavita paragraph two.</p>';
    const second = kavitaDom.window.document.getElementById('kavita-two');
    second.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });
    deadline = Date.now() + 2000;
    while (!payloads.some(payload => payload.paragraphs.includes('Kavita paragraph two.'))
            && Date.now() < deadline) await wait(20);
    assert(payloads.some(payload => payload.paragraphs.includes('Kavita paragraph two.')),
        'Angular innerHtml replacement must trigger bounded rediscovery');

    const callsBeforeManga = payloads.length;
    kavitaDom.window.history.pushState({}, '', '/library/7/series/42/manga/99');
    kavitaDom.window.dispatchEvent(new kavitaDom.window.CustomEvent('bt:reader-route'));
    await wait(50);
    assert.strictEqual(kavitaDom.window.document.getElementById('bt-bar').hidden, true,
        'The overlay must deactivate outside Kavita EPUB routes');
    root.innerHTML = '<p>Manga route text must stay local.</p>';
    await wait(400);
    assert.strictEqual(payloads.length, callsBeforeManga,
        'Kavita manga/PDF routes must never send content for translation');
    kavitaDom.window.close();
}

async function runTest() {
    console.log("Starting frontend assertions test...");
    await assertManagedLoaderContract();
    
    // 1. Initial page load will trigger visible queue (1 chunk) then prefetch queue (3 chunks).
    // Let's provide a 429 response first for the visible request.
    fetchResponses.push({
        status: 429,
        body: {
            error: 'rate_limited', retry_after: 1,
            retry_safe: true, scope: 'api_admission'
        } // wait 1s
    });
    
    await wait(800);
    
    // Wait for the UI state to update after 429
    const btBar = dom.window.document.getElementById('bt-bar');
    const statusText = dom.window.document.getElementById('bt-status-text').textContent;
    
    console.log("Status text after 429:", statusText);
    assert(btBar.dataset.state === 'ratelimit', 'State should be ratelimit');
    // Copy is intentionally short (see i18n): "Waiting {n}s…". The point of this
    // assertion is that a 429 shows a benign waiting state, not a fatal error —
    // matched via the ratelimit state above plus the countdown text below.
    assert(/waiting/i.test(statusText) && !statusText.includes('Error'), 'Should show a benign waiting message, not a fatal error');
    assert(maxActiveFetches <= 1, 'Only one fetch active at a time');
    
    // Provide a valid response for when it resumes (for visible 1)
    fetchResponses.push({
        status: 200,
        body: { translations: ["Translated visible 1"] }
    });
    // Provide a valid response for visible 2
    fetchResponses.push({
        status: 200,
        body: { translations: ["Translated visible 2"] }
    });
    
    // Provide one bounded response for the remaining prefetch block
    fetchResponses.push({
        status: 200,
        body: { translations: [
            "Translated prefetch 1", "Translated prefetch 2",
            "Translated prefetch 3", "Translated prefetch 4"
        ] }
    });
    
    const timeBeforeResume = Date.now();
    await wait(3000); // Wait for the 1s retry_after + gap + all fetches to complete

    // The second and third fetch calls should happen AFTER the retry_after delay
    assert(fetchCalls.length >= 2, 'Queue should resume after 429');
    
    const delay = fetchCalls[1].time - fetchCalls[0].time;
    console.log("Delay before retry (ms):", delay);
    assert(delay >= 950, 'Retry-After delay should be honored approximately'); // >= 1000ms theoretically
    
    // Look at the bodies of the first few fetch calls to ensure visible happens before prefetch
    const call1Body = JSON.parse(fetchCalls[0].options.body); // was 429
    const call2Body = JSON.parse(fetchCalls[1].options.body); // visible 1 retry
    const call3Body = JSON.parse(fetchCalls[2].options.body); // visible 2
    
    assert(call1Body.paragraphs[0] === 'visible 1', 'First fetch should be visible 1');
    assert(call2Body.paragraphs[0] === 'visible 1', 'Second fetch (retry) should be visible 1');
    assert(call3Body.paragraphs[0] === 'visible 2', 'Third fetch should be visible 2');
    
    const allFetchedParagraphs = fetchCalls.map(c => JSON.parse(c.options.body).paragraphs).flat();
    
    fetchCalls.forEach((c, i) => {
        console.log(`Fetch ${i}:`, JSON.parse(c.options.body).paragraphs);
    });

    fetchCalls.forEach((c) => {
        const body = JSON.parse(c.options.body);
        assert(typeof body.book_id === 'string' && body.book_id.length > 0,
            'Every translation request must carry a bounded book cache scope');
        assert(typeof body.chapter_id === 'string' && body.chapter_id.length > 0,
            'Every translation request must carry a bounded chapter cache scope');
        assert.strictEqual(c.options.credentials, 'same-origin',
            'Cookie credentials must not be sent cross-origin without explicit opt-in');
        assert.strictEqual(body.allow_cloud_fallback, false,
            'Cloud fallback consent must be false unless the reader explicitly opts in');
    });
    
    // visible 1, visible 1, visible 2, prefetch 1, prefetch 2, prefetch 3, prefetch 4
    // We expect 7 paragraphs in total passed to fetch
    const uniqueParagraphs = new Set(allFetchedParagraphs);
    console.log("All fetched paragraphs:", allFetchedParagraphs);
    
    // Ensure no accidental duplicates beyond the retry
    const nonRetryParagraphs = allFetchedParagraphs.slice(1);
    const hasDups = new Set(nonRetryParagraphs).size !== nonRetryParagraphs.length;
    assert(!hasDups, 'There should be no duplicate translation blocks requested');
    
    assert(maxActiveFetches === 1, 'Never exceeded one active fetch');

    // An ambiguous transport failure must not automatically create duplicate
    // provider work. It remains failed until the user explicitly retries.
    const failedParagraph = iframeDoc.createElement('p');
    failedParagraph.textContent = 'network failure';
    failedParagraph.getBoundingClientRect = () => ({
        width: 100, height: 20, left: 0, top: 0
    });
    fetchResponses.push(new Error('synthetic network failure'));
    iframeDoc.querySelector('section').appendChild(failedParagraph);
    await wait(1800);
    const failureCalls = fetchCalls.filter(c =>
        JSON.parse(c.options.body).paragraphs.includes('network failure'));
    assert.strictEqual(failureCalls.length, 1,
        'Ambiguous transport failures must wait for an explicit user retry');
    assert.strictEqual(btBar.dataset.state, 'error',
        'Terminal transport failures must surface an actionable error state');

    // Regression guard for the status-bar flicker bug: the position-based
    // page-turn poll (every 350ms) used to run unconditionally, including
    // while a translation pass was actively inserting bilingual blocks --
    // which changes paragraph heights and can shift which element counts as
    // "first visible" with no real page turn. That false positive forced a
    // full newGeneration() reset (hides the status pill) immediately followed
    // by a fresh translateCurrentPage() (shows it again) on every poll tick
    // for as long as work was in progress -- visible as the status pill
    // blinking on/off rapidly. The fix gates that poll on genuinely being
    // idle; real navigation while work is in flight is still caught
    // immediately via the epub.js relocated/rendered hooks, which don't
    // depend on visual position. A full behavioral reproduction needs a
    // real layout engine (jsdom does not compute live reflow from DOM
    // changes), so this locks the source-level guard in place instead.
    assert(
        /if\s*\(\s*!isTranslating\s*&&\s*!isPrefetching\s*\)\s*\{[\s\S]{0,400}?getVisibleParagraphs/.test(code),
        'Page-turn poll must be gated on being idle (regression: status-bar flicker while translating)'
    );

    // Regression guard: "Translated" mode used to store only PLAIN TEXT of the
    // original and restore via textContent, permanently stripping the
    // paragraph's markup (italics/bold/links) when toggling back. The fix
    // keeps cloned DOM nodes in a WeakMap and restores them without reparsing
    // serialized EPUB markup through innerHTML.
    assert(
        /const originalContent = new WeakMap\(\)/.test(code)
            && /function restoreOriginal\(el\)/.test(code)
            && /originalContent\.set\(el,\s*fragment\)/.test(code)
            && /el\.replaceChildren\(/.test(code)
            && !/el\.innerHTML\s*=/.test(code),
        'Inline mode must restore cloned markup without an innerHTML parser sink'
    );

    assert(
        /let prefetchEnabled = localStorage\.getItem\(['"]bt_prefetch['"]\) === ['"]1['"]/.test(code),
        'Whole-chapter prefetch must be explicit opt-in, not the default'
    );

    // Regression guard: ambiguous timeouts/network failures must never spawn a
    // second provider request automatically. Only admission-rejected 429s may
    // requeue, and those responses have a strict bound.
    assert(
        !/requeueForRetry/.test(code)
            && /function|const markBatchFailed/.test(code)
            && /requeueRateLimited/.test(code)
            && /BT_CLIENT_MAX_RATE_LIMIT_RESPONSES/.test(code),
        'Only bounded 429 admission retries may be automatic'
    );

    // Regression guard: the client safety-net timeout must be distinguishable
    // from a deliberate abort (mode/language/page change), so timeouts become
    // visible terminal failures while deliberate aborts discard stale work.
    assert(
        /btTimedOut/.test(code) && /'timeout'/.test(code) && /'aborted'/.test(code),
        'Timeout failures must be distinguished from deliberate aborts'
    );

    assert(
        /const PERSIST_CACHE = cfg\.persistCache === true/.test(code)
            && /function cacheKeyForText\(text/.test(code)
            && /function elementContextId\(el\)/.test(code)
            && /scope\.book_id, scope\.chapter_id, elementContext, text/.test(code)
            && /BT_UI_VERSION, SOURCE_LANG, TARGET_LANG/.test(code)
            && !/BT_UI_VERSION, generation, SOURCE_LANG, TARGET_LANG/.test(code),
        'Persistent browser caching must be opt-in and keys must be context-scoped'
    );

    await assertProviderPolicyControls();
    await assertStalePolicyIsRefetchedWithoutTranslationReplay();
    await assertAmbiguous429IsTerminal();
    await assertConfiguredBatchSizeIsUsed();
    await assertSafe429RetryBoundSurvivesRediscovery();
    await assertVisibleWorkInterruptsPrefetchDelay();

    const [tokenTransport, forwardedTransport, cwaTransport, consentedTransport] = await Promise.all([
        captureAuthTransport({ authMode: 'token', apiToken: 'browser-token' }),
        captureAuthTransport({ authMode: 'forwarded', apiToken: 'must-not-leak' }),
        captureAuthTransport({ authMode: 'cwa_session', sendCredentials: true }),
        captureAuthTransport({ authMode: 'cwa_session' }, true)
    ]);
    assert.strictEqual(tokenTransport.credentials, 'omit',
        'Token mode must omit CWA cookies');
    assert.strictEqual(tokenTransport.headers['X-BT-Token'], 'browser-token',
        'Token mode must send only its explicit compatibility token');
    assert.strictEqual(forwardedTransport.credentials, 'include',
        'Forwarded mode must present the SSO cookie to the same-origin identity edge');
    assert(!('X-BT-Token' in forwardedTransport.headers),
        'Forwarded mode must not leak an accidentally configured token');
    assert.strictEqual(cwaTransport.credentials, 'include',
        'Explicit cross-origin CWA-session mode must include its HttpOnly cookie');
    assert(!('X-BT-Token' in cwaTransport.headers),
        'CWA-session mode must not send a compatibility token');
    assert.strictEqual(JSON.parse(tokenTransport.body).allow_cloud_fallback, false,
        'A fresh reader must not consent to cloud fallback');
    assert.deepStrictEqual(JSON.parse(tokenTransport.body).provider_policy, {
        primary: 'local',
        fallback: 'remote',
        generation: '0123456789abcdef0123456789abcdef'
    }, 'Every official-reader translation must bind the current provider policy');
    assert.strictEqual(JSON.parse(consentedTransport.body).allow_cloud_fallback, true,
        'The explicit privacy control must consent only subsequent requests');
    assert(!/localStorage\.(?:getItem|setItem)\([^)]*cloud/i.test(code),
        'Cloud fallback consent must never persist across reader sessions');

    await assertKavitaSessionLoaderContract();
    await assertKavitaDirectLoaderContract();
    await assertKavitaReaderAdapterContract();

    console.log("All assertions passed.");
    process.exit(0);
}

runTest().catch(err => {
    console.error("Test failed:", err);
    process.exit(1);
});
