/**
 * book-translator — stock-reader proxy bootstrap.
 *
 * nginx injects only this file. It validates the server-owned browser
 * contract, stays inert off supported reader routes, exchanges raw reader
 * proof for a short-lived HttpOnly plugin session, and only then loads the
 * overlay assets.
 */
(function () {
    'use strict';
    if (window.__BT_LOADER_RAN) { return; }
    window.__BT_LOADER_RAN = true;

    var VERSION = (function () {
        try {
            var src = document.currentScript && document.currentScript.src;
            if (!src) { return 'dev'; }
            return new URL(src, window.location.href).searchParams.get('v') || 'dev';
        } catch (e) { return 'dev'; }
    })();
    var BASE = '/bt-static/';
    var managedConfig = null;
    var assetsLoaded = false;
    var routeWasSupported = false;
    var sessionPromise = null;
    var sessionRefreshTimer = null;

    function exactKavitaRoute(pathname) {
        return /^\/library\/[1-9][0-9]*\/series\/[1-9][0-9]*\/book\/[1-9][0-9]*\/?$/.test(pathname);
    }

    function exactCwaRoute(pathname) {
        return /^\/read\/[^/?#]+(?:\/[^?#]*)?\/?$/.test(pathname);
    }

    function isSupportedRoute(managed) {
        var readerType = managed && managed.readerType || 'cwa';
        return readerType === 'kavita'
            ? exactKavitaRoute(window.location.pathname)
            : exactCwaRoute(window.location.pathname);
    }

    function optionalBoundedInteger(managed, name, minimum, maximum, fallback) {
        if (managed[name] === undefined) { return fallback; }
        if (!Number.isInteger(managed[name])
                || managed[name] < minimum || managed[name] > maximum) {
            throw new Error('unsupported browser scheduling contract');
        }
        return managed[name];
    }

    function validateManagedConfig(managed) {
        var expectedCredentials = {
            cwa_session: 'same-origin',
            reader_session: 'same-origin',
            forwarded: 'include'
        };
        if (!managed || managed.apiUrl !== '/bt-api'
                || expectedCredentials[managed.authMode] !== managed.credentials) {
            throw new Error('unsupported browser authentication contract');
        }
        var batchSize = optionalBoundedInteger(
            managed, 'batchSize', 1, 50, 5
        );
        var prefetchGapMs = optionalBoundedInteger(
            managed, 'prefetchGapMs', 0, 10000, 0
        );
        var readerType = managed.readerType || 'cwa';
        if (readerType !== 'cwa' && readerType !== 'kavita') {
            throw new Error('unsupported reader authentication contract');
        }

        if (managed.authMode !== 'reader_session') {
            if (readerType === 'cwa') {
                return {
                    apiUrl: managed.apiUrl,
                    authMode: managed.authMode,
                    credentials: managed.credentials,
                    readerType: 'cwa',
                    readerVersion: '',
                    readerContractVersion: 'cwa-epub-v1',
                    batchSize: batchSize,
                    prefetchGapMs: prefetchGapMs
                };
            }
            var rVer = managed.readerVersion || '0.9.1.4';
            var validKavitaVer = rVer === '0.9.0.2' || /^0\.9\.[0-9]+(\.[0-9]+)?$/.test(rVer);
            var validKavitaContract = (managed.readerContractVersion === 'kavita-0.9.0.2-epub-v1'
                || managed.readerContractVersion === 'kavita-epub-v1'
                || !managed.readerContractVersion);
            if (!validKavitaVer || !validKavitaContract) {
                throw new Error('unsupported reader version contract');
            }
            return {
                apiUrl: managed.apiUrl,
                authMode: managed.authMode,
                credentials: managed.credentials,
                readerType: 'kavita',
                readerVersion: rVer,
                readerContractVersion: managed.readerContractVersion || 'kavita-0.9.0.2-epub-v1',
                batchSize: batchSize,
                prefetchGapMs: prefetchGapMs
            };
        }

        var validCwaVersion = readerType === 'cwa'
            && (managed.readerVersion === '3.1.4'
                || /^4\.[0-9]+\.[0-9]+$/.test(managed.readerVersion))
            && managed.readerContractVersion === 'cwa-epub-v1';
        var validKavitaVersion = readerType === 'kavita'
            && (managed.readerVersion === '0.9.0.2'
                || /^0\.9\.[0-9]+(\.[0-9]+)?$/.test(managed.readerVersion))
            && (managed.readerContractVersion === 'kavita-0.9.0.2-epub-v1'
                || managed.readerContractVersion === 'kavita-epub-v1');
        if (!validCwaVersion && !validKavitaVersion) {
            throw new Error('unsupported reader version contract');
        }
        return {
            apiUrl: managed.apiUrl,
            authMode: managed.authMode,
            credentials: managed.credentials,
            readerType: readerType,
            readerVersion: managed.readerVersion,
            readerContractVersion: managed.readerContractVersion,
            batchSize: batchSize,
            prefetchGapMs: prefetchGapMs
        };
    }

    function kavitaAccessToken() {
        if (!managedConfig || managedConfig.readerType !== 'kavita') { return ''; }
        try {
            var raw = localStorage.getItem('kavita-user');
            if (!raw || raw.length > 32768) { return ''; }
            var user = JSON.parse(raw);
            var token = user && typeof user === 'object' && !Array.isArray(user)
                ? user.token : '';
            if (typeof token !== 'string' || token.length < 1 || token.length > 8192
                    || /[^\x21-\x7e]/.test(token)) {
                return '';
            }
            return token;
        } catch (e) {
            return '';
        }
    }

    function scheduleSessionRefresh(expiresIn) {
        if (sessionRefreshTimer) { clearTimeout(sessionRefreshTimer); }
        var delay = Math.max(1000, (expiresIn - 60) * 1000);
        sessionRefreshTimer = setTimeout(function () {
            sessionRefreshTimer = null;
            exchangeReaderSession().catch(function (error) {
                console.error('[BookTranslator] session refresh failed:', error.message);
            });
        }, delay);
    }

    function exchangeReaderSession() {
        if (!managedConfig || managedConfig.authMode !== 'reader_session') {
            return Promise.resolve();
        }
        if (!isSupportedRoute(managedConfig)) {
            return Promise.reject(new Error('reader route is not active'));
        }
        if (sessionPromise) { return sessionPromise; }

        var headers = { Accept: 'application/json' };
        var accessToken = kavitaAccessToken();
        if (accessToken) { headers.Authorization = 'Bearer ' + accessToken; }
        sessionPromise = fetch('/bt-api/session', {
            method: 'POST',
            credentials: 'same-origin',
            cache: 'no-store',
            redirect: 'error',
            headers: headers
        }).then(function (response) {
            if (!response.ok) { throw new Error('reader session unavailable'); }
            return response.json();
        }).then(function (payload) {
            var expiresIn = payload && Number(payload.expires_in);
            if (!payload || payload.status !== 'ok'
                    || payload.reader_type !== managedConfig.readerType
                    || payload.reader_version !== managedConfig.readerVersion
                    || !Number.isInteger(expiresIn)
                    || expiresIn < 1 || expiresIn > 300) {
                throw new Error('invalid reader session response');
            }
            scheduleSessionRefresh(expiresIn);
        }).finally(function () {
            sessionPromise = null;
        });
        return sessionPromise;
    }

    function installManagedConfig() {
        var existing = window.BOOK_TRANSLATOR || {};
        window.BOOK_TRANSLATOR = {
            apiUrl: managedConfig.apiUrl,
            sourceLang: existing.sourceLang || 'Auto',
            targetLang: existing.targetLang || '',
            bookId: existing.bookId,
            persistCache: existing.persistCache === true,
            authMode: managedConfig.authMode,
            credentials: managedConfig.credentials,
            readerType: managedConfig.readerType,
            readerVersion: managedConfig.readerVersion,
            readerContractVersion: managedConfig.readerContractVersion,
            batchSize: managedConfig.batchSize,
            prefetchGapMs: managedConfig.prefetchGapMs,
            apiToken: ''
        };
    }

    function loadAssets() {
        if (assetsLoaded) { return; }
        assetsLoaded = true;
        installManagedConfig();

        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = BASE + 'translator.css?v=' + VERSION;
        (document.head || document.documentElement).appendChild(link);

        var script = document.createElement('script');
        script.src = BASE + 'translator.js?v=' + VERSION;
        script.defer = true;
        (document.head || document.documentElement).appendChild(script);
    }

    function announceRoute() {
        try {
            window.dispatchEvent(new CustomEvent('bt:reader-route'));
        } catch (e) { /* old browser: interval-based adapter remains safe */ }
    }

    function activateRoute() {
        if (!managedConfig) { return; }
        var supported = isSupportedRoute(managedConfig);
        if (!supported) {
            routeWasSupported = false;
            if (sessionRefreshTimer) {
                clearTimeout(sessionRefreshTimer);
                sessionRefreshTimer = null;
            }
            announceRoute();
            return;
        }
        if (routeWasSupported) {
            announceRoute();
            return;
        }
        routeWasSupported = true;
        var ready = managedConfig.authMode === 'reader_session'
            ? exchangeReaderSession()
            : Promise.resolve();
        ready.then(function () {
            if (!isSupportedRoute(managedConfig)) { return; }
            if (!assetsLoaded) { loadAssets(); }
            else { announceRoute(); }
        }).catch(function (error) {
            routeWasSupported = false;
            console.error('[BookTranslator] disabled:', error.message);
        });
    }

    function installSpaHooks() {
        ['pushState', 'replaceState'].forEach(function (name) {
            var original = history[name];
            history[name] = function () {
                var result = original.apply(this, arguments);
                activateRoute();
                return result;
            };
        });
        window.addEventListener('popstate', activateRoute);
    }

    installSpaHooks();
    fetch('/bt-config.json', {
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: { Accept: 'application/json' }
    }).then(function (response) {
        if (!response.ok) { throw new Error('browser configuration unavailable'); }
        return response.json();
    }).then(function (managed) {
        managedConfig = validateManagedConfig(managed);
        if (managedConfig.authMode === 'reader_session') {
            window.__BT_REFRESH_SESSION = exchangeReaderSession;
        }
        activateRoute();
    }).catch(function (error) {
        console.error('[BookTranslator] disabled:', error.message);
    });
})();

