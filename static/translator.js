/**
 * book-translator — Calibre-Web-Automated Translation Overlay
 */

(function () {
    'use strict';
    // ── Version & Telemetry ──────────────────────────────────────────
    const BT_UI_VERSION = '2.3.3';
    console.log(`[BookTranslator] loaded version ${BT_UI_VERSION}`);
    const cfg = (typeof window !== 'undefined' && window.BOOK_TRANSLATOR) || {};
    function boundedInteger(value, minimum, maximum, fallback) {
        return Number.isInteger(value) && value >= minimum && value <= maximum
            ? value : fallback;
    }
    const configuredReaderType = cfg.readerType || '';
    const READER_TYPE = configuredReaderType === 'kavita' ? 'kavita' : 'cwa';
    const STRICT_READER_ROUTE = configuredReaderType === 'cwa'
        || configuredReaderType === 'kavita';
    const validKavitaVersion = cfg.readerVersion === '0.9.0.2'
        || /^0\.9\.[0-9]+(\.[0-9]+)?$/.test(cfg.readerVersion || '');
    const validKavitaContract = READER_TYPE === 'kavita'
        && validKavitaVersion
        && (cfg.readerContractVersion === 'kavita-0.9.0.2-epub-v1'
            || cfg.readerContractVersion === 'kavita-epub-v1');
    if (configuredReaderType && configuredReaderType !== 'cwa'
            && configuredReaderType !== 'kavita') {
        console.error('[BookTranslator] disabled: unsupported reader type');
        return;
    }
    if (READER_TYPE === 'kavita' && !validKavitaContract) {
        console.error('[BookTranslator] disabled: unsupported Kavita reader contract');
        return;
    }
    const configuredAuthMode = cfg.authMode || (cfg.apiToken ? 'token' : 'cwa_session');
    const AUTH_MODE = ['cwa_session', 'reader_session', 'token', 'forwarded'].includes(configuredAuthMode)
        ? configuredAuthMode
        : 'cwa_session';
    const configuredCredentials = ['omit', 'same-origin', 'include'].includes(cfg.credentials)
        ? cfg.credentials
        : null;
    const TRANSLATOR_URL = (cfg.apiUrl && cfg.apiUrl.length)
        ? cfg.apiUrl
        : (window.location.protocol === 'https:' ? null : `http://${window.location.hostname}:8390`);
    // ── Per-book preferences (additive) ──────────────────────────────
    // Mode and languages are remembered per book and fall back to the
    // pre-existing global keys, which stay the default for new books and
    // keep older stored values working. Every write below updates the
    // global key first (unchanged contract) and then the per-book key.
    function bookScopeId() {
        try { return currentBookId(); } catch (e) { return 'unscoped'; }
    }
    function bookPrefGet(name) {
        try {
            const scoped = localStorage.getItem('bt_book_' + bookScopeId() + '_' + name);
            if (scoped !== null && scoped !== undefined) return scoped;
        } catch (e) { /* storage may be unavailable */ }
        try { return localStorage.getItem(name); } catch (e) { return null; }
    }
    function bookPrefRemember(name, value) {
        try { localStorage.setItem('bt_book_' + bookScopeId() + '_' + name, value); }
        catch (e) { /* storage may be unavailable */ }
    }

    // ── Overlay style preset (additive plumbing) ─────────────────────
    // One of default/contrast/large. Stored globally, applied as a data
    // attribute the stylesheet themes off; unknown values reset to default.
    const STYLE_PRESETS = ['default', 'contrast', 'large'];
    let stylePreset = 'default';
    try {
        const storedPreset = localStorage.getItem('bt_style_preset');
        if (STYLE_PRESETS.indexOf(storedPreset) !== -1) stylePreset = storedPreset;
    } catch (e) { /* storage may be unavailable */ }
    function setStylePreset(preset) {
        if (STYLE_PRESETS.indexOf(preset) === -1) preset = 'default';
        stylePreset = preset;
        try { localStorage.setItem('bt_style_preset', preset); } catch (e) {}
        try { document.documentElement.dataset.btPreset = preset; } catch (e) {}
        const presetBar = document.getElementById('bt-bar');
        if (presetBar) presetBar.dataset.preset = preset;
    }

    let SOURCE_LANG_SETTING = bookPrefGet('bt_source_lang') || cfg.sourceLang || 'Auto';
    let detectedSourceLang = null;

    function resolveEffectiveSourceLang(doc) {
        if (SOURCE_LANG_SETTING !== 'Auto' && availableLangCodes.has(SOURCE_LANG_SETTING)) {
            return SOURCE_LANG_SETTING;
        }
        if (!detectedSourceLang && doc) {
            detectedSourceLang = detectBookLanguage(doc);
        }
        return detectedSourceLang || 'English';
    }

    let SOURCE_LANG = resolveEffectiveSourceLang(null);

    // Map browser language codes to the full language name the backend expects
    // (used only to pick a sensible default target on first run).
    const langMap = {
        'es': 'Spanish', 'en': 'English', 'fr': 'French', 'de': 'German',
        'pt': 'Portuguese', 'it': 'Italian', 'ru': 'Russian', 'zh': 'Chinese',
        'ja': 'Japanese', 'hi': 'Hindi', 'ar': 'Arabic', 'bn': 'Bengali',
        'ur': 'Urdu', 'ko': 'Korean', 'tr': 'Turkish', 'pl': 'Polish',
        'nl': 'Dutch', 'uk': 'Ukrainian', 'vi': 'Vietnamese', 'th': 'Thai',
        'id': 'Indonesian', 'fa': 'Persian', 'he': 'Hebrew', 'el': 'Greek',
        'cs': 'Czech', 'sv': 'Swedish', 'da': 'Danish', 'fi': 'Finnish',
        'no': 'Norwegian', 'nb': 'Norwegian', 'hu': 'Hungarian',
        'ro': 'Romanian', 'ms': 'Malay', 'ta': 'Tamil', 'te': 'Telugu',
        'mr': 'Marathi', 'gu': 'Gujarati', 'pa': 'Punjabi', 'sw': 'Swahili',
        'tl': 'Tagalog', 'ca': 'Catalan', 'bg': 'Bulgarian', 'sk': 'Slovak',
        'sr': 'Serbian', 'hr': 'Croatian', 'sl': 'Slovenian', 'lt': 'Lithuanian',
        'lv': 'Latvian', 'et': 'Estonian'
    };

    const browserCode = (navigator.language || 'es').split('-')[0];
    const defaultLang = langMap[browserCode] || 'Spanish';
    let TARGET_LANG = bookPrefGet('bt_lang') || cfg.targetLang || defaultLang;

    const BT_CLIENT_MAX_INFLIGHT = 1;
    const BT_CLIENT_RATE_LIMIT_BACKOFF_MS = 10000;
    const BT_CLIENT_MAX_RATE_LIMIT_RESPONSES = 3;
    const BT_CLIENT_MAX_RETRY_AFTER_SECONDS = 60;

    let translationMode = bookPrefGet('bt_mode') || 'off'; // 'off', 'bilingual', 'translated'
    let isTranslating = false;
    let isPrefetching = false;
    let visibleQueue = [];
    let prefetchQueue = [];
    let isPumpRunning = false;
    // Offline-first: while true the pump issues no network work. Queues and
    // persisted translations stay intact, so already-translated content
    // keeps rendering and pending work resumes on 'online'.
    // NOTE: deliberately no Service Worker. This overlay is injected into
    // stock reader pages — a SW registered here would claim scope over the
    // host reader origin and intercept reader traffic. Offline resilience
    // therefore means gating our own pump, not owning the network layer.
    let isOffline = typeof navigator !== 'undefined' && navigator.onLine === false;
    let rateLimitUntil = 0;
    let nextPrefetchAt = 0;
    let prefetchWaitWake = null;
    let firstVisibleBatchCompleted = false;
    let lastFirstVisibleHash = null;
    let pendingFirstVisibleHash = null; // 2-poll debounce for the page-turn detector

    function kavitaRouteParts() {
        const match = window.location.pathname.match(
            /^\/library\/([1-9][0-9]*)\/series\/([1-9][0-9]*)\/book\/([1-9][0-9]*)\/?$/
        );
        return match ? { libraryId: match[1], seriesId: match[2], chapterId: match[3] } : null;
    }

    function isSupportedReaderRoute() {
        if (!STRICT_READER_ROUTE) return true;
        if (READER_TYPE === 'kavita') return kavitaRouteParts() !== null;
        return /^\/read\/[^/?#]+(?:\/[^?#]*)?\/?$/.test(window.location.pathname);
    }

    let readerRouteActive = false;

    // UI / status state
    let prefetchEnabled = localStorage.getItem('bt_prefetch') === '1'; // default ON for zero-wait reading
    // Privacy decision scoped to this reader tab/book. Never restore it from
    // browser storage: every new book session starts with remote fallback off.
    let allowCloudFallback = false;
    // Fetched from the authenticated API. Until this is known, translation is
    // fail-closed so book text cannot leave the browser without an honest UI.
    let providerPolicyState = null;
    let providerPolicyPromise = null;
    // Honest chapter progress: `chapterDone` counts paragraphs actually
    // processed this session (visible AND prefetch), `inflightCount` the ones
    // currently at the API. The displayed total is computed live as
    // done + inflight + still-queued, so it stays truthful when queues are
    // re-filtered (cached/stale items dropped) or re-triggered on page turns.
    let chapterDone = 0;
    let inflightCount = 0;
    let errorCount = 0;       // consecutive failed requests (drives the error state)
    const failedParagraphs = new Set(); // terminal until an explicit user retry
    // Queue objects are rebuilt whenever the reader DOM is rediscovered. Keep
    // admission retry counts outside those transient objects so DOM mutations
    // cannot reset the bound within one generation.
    const rateLimitResponses = new Map(); // paragraph hash -> response count
    let doneHideTimer = null;
    let lastTriggerReason = 'init'; // why translateCurrentPage last ran (shown in the debug menu)

    // ── Browser translation cache ──────────────────────────────────────
    // Persistence is privacy-sensitive on a shared browser: another CWA user
    // could otherwise inherit translated book text after logout. Keep the
    // cache in memory by default; operators may explicitly opt in with
    // BOOK_TRANSLATOR.persistCache=true. Backend SQLite remains the durable,
    // server-side cache (and is tenant-isolated when identity auth is enabled).
    const PERSIST_CACHE = cfg.persistCache === true;
    const CACHE_PREFIX = 'bt_cache_v3_';
    const LEGACY_CACHE_PREFIX = 'bt_cache_v2_';
    const CACHE_MAX_ENTRIES = 5000;      // safety cap to stay under the localStorage quota

    // v2 keys omitted book/chapter/source/prompt context. Remove them rather
    // than risk rendering a stale or cross-user translation after upgrade.
    try {
        Object.keys(localStorage).filter(k => k.startsWith(LEGACY_CACHE_PREFIX))
            .forEach(k => localStorage.removeItem(k));
    } catch (e) { /* storage may be unavailable */ }

    function loadCacheForLang(lang) {
        if (!PERSIST_CACHE) return {};
        try {
            const raw = localStorage.getItem(CACHE_PREFIX + lang);
            if (raw) return JSON.parse(raw) || {};
        } catch (e) { /* ignore corrupt/missing cache */ }
        return {};
    }

    let persistTimer = null;
    function schedulePersist() {
        if (!PERSIST_CACHE) return;
        if (persistTimer) return;
        persistTimer = setTimeout(persistCacheNow, 1500);
    }
    function persistCacheNow() {
        if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; }
        if (!PERSIST_CACHE) return;
        try {
            let toPersist = translatedParagraphs;
            const keys = Object.keys(translatedParagraphs);
            if (keys.length > CACHE_MAX_ENTRIES) {
                // Object string-keys keep insertion order: keep the most recent N for localStorage.
                toPersist = {};
                for (const k of keys.slice(keys.length - CACHE_MAX_ENTRIES)) toPersist[k] = translatedParagraphs[k];
            }
            localStorage.setItem(CACHE_PREFIX + TARGET_LANG, JSON.stringify(toPersist));
        } catch (e) {
            // Quota exceeded — drop the oldest half and retry once in localStorage,
            // preserving in-memory translatedParagraphs for active reading session.
            try {
                const keys = Object.keys(translatedParagraphs);
                const trimmed = {};
                for (const k of keys.slice(Math.floor(keys.length / 2))) trimmed[k] = translatedParagraphs[k];
                localStorage.setItem(CACHE_PREFIX + TARGET_LANG, JSON.stringify(trimmed));
            } catch (e2) { /* give up persisting; in-memory cache still works */ }
        }
    }


    // ── High-Capacity IndexedDB Cache ──────────────────────────────────
    const IDB_NAME = 'BookTranslatorDB';
    const IDB_STORE = 'translations_v1';
    let idbPromise = null;

    function getIDB() {
        if (!idbPromise && window.indexedDB) {
            idbPromise = new Promise((resolve) => {
                try {
                    const req = window.indexedDB.open(IDB_NAME, 1);
                    req.onupgradeneeded = (e) => {
                        const db = e.target.result;
                        if (!db.objectStoreNames.contains(IDB_STORE)) {
                            db.createObjectStore(IDB_STORE, { keyPath: 'key' });
                        }
                    };
                    req.onsuccess = (e) => resolve(e.target.result);
                    req.onerror = () => resolve(null);
                } catch (e) {
                    resolve(null);
                }
            });
        }
        return idbPromise;
    }

    async function loadCacheAsync(lang) {
        if (!PERSIST_CACHE) return;
        try {
            const db = await getIDB();
            if (!db) return;
            const tx = db.transaction(IDB_STORE, 'readonly');
            const store = tx.objectStore(IDB_STORE);
            const req = store.getAll();
            req.onsuccess = () => {
                if (req.result && Array.isArray(req.result)) {
                    for (let i = 0; i < req.result.length; i++) {
                        const item = req.result[i];
                        if (item && item.key && item.text) {
                            translatedParagraphs[item.key] = item.text;
                        }
                    }
                }
            };
        } catch (e) {}
    }

    let translatedParagraphs = loadCacheForLang(TARGET_LANG);
    loadCacheAsync(TARGET_LANG); // hash -> text (restored from last session)

    // ── In-flight request control (responsive buttons + language switches) ──
    // `generation` is bumped whenever the user changes mode/language so that
    // stale in-flight responses are ignored. It is deliberately not part of a
    // translation cache key: page rediscovery is a transport lifecycle event,
    // not a semantic prompt change. `activeControllers` lets us abort pending
    // fetches immediately instead of blocking the UI until they finish.
    let generation = 0;
    const activeControllers = new Set();
    let paragraphTextCache = new WeakMap();

    function newGeneration() {
        generation++;
        invalidateParagraphsCache();
        rateLimitResponses.clear();
        if (prefetchWaitWake) prefetchWaitWake();
        for (const c of activeControllers) {
            try { c.abort(); } catch (e) { /* ignore */ }
        }
        activeControllers.clear();
        paragraphTextCache = new WeakMap();
        visibleQueue = [];
        prefetchQueue = [];
        chapterDone = 0;
        inflightCount = 0;
        isTranslating = false;
        isPrefetching = false;
        firstVisibleBatchCompleted = false;
        refreshStatus();
        return generation;
    }

    function waitForPrefetchGap(milliseconds) {
        return new Promise(resolve => {
            let settled = false;
            let timer = null;
            const finish = () => {
                if (settled) return;
                settled = true;
                if (timer !== null) clearTimeout(timer);
                if (prefetchWaitWake === finish) prefetchWaitWake = null;
                resolve();
            };
            prefetchWaitWake = finish;
            timer = setTimeout(finish, milliseconds);
        });
    }

    function chapterProgress() {
        // done / total for the current chapter session. Total is live:
        // processed + in flight + still queued — never a stale snapshot.
        const total = chapterDone + inflightCount + visibleQueue.length + prefetchQueue.length;
        return { done: chapterDone, total: total };
    }

    function isBadTranslation(tr) {
        // Treat backend error markers and empty results as "not translated" so
        // they are neither rendered nor cached client-side. Automatic retries
        // are unsafe after an ambiguous timeout; the user can retry explicitly.
        return !tr || typeof tr !== 'string'
            || tr.startsWith('[TRANSLATION ERROR')
            || tr.startsWith('[ERROR');
    }

    function renderMode(elements) {
        if (translationMode === 'bilingual') showTranslationsBilingual(elements);
        else if (translationMode === 'translated') showTranslationsInline('translated', elements);
    }

    // ── i18n ───────────────────────────────────────────────────────────
    const strings = {
        en: {
            off: 'Original', bilingual: 'Bilingual', translated: 'Translated',
            translatingPage: 'Translating…', translatingChapter: 'Chapter', done: '✓ Ready', error: '⚠ Retry',
            rateLimited: 'Waiting {n}s…',
            retrying: 'Retrying…',
            restoring: 'Restoring saved translations…',
            cycleHint: 'Click to cycle: Original → Bilingual → Translated', langHint: 'Target language', sourceLangHint: 'Source language', topLanguages: 'Most spoken', allLanguages: 'All languages (A–Z)', settings: 'Settings',
            prefetchWhole: 'Smooth reading (lookahead prefetch)', clearLang: 'Clear this language\'s cache', clearAll: 'Clear all cache',
            cached: 'Cached', cleared: 'Cache cleared',
            bookTranslator: 'Book Translator', modeLabel: 'Mode', sourceLabel: 'Source', targetLabel: 'Target', langLabel: 'Language',
            retryPage: 'Retry current page', debug: 'Debug',
            cloudFallback: 'Allow cloud fallback',
            cloudPrivacy: 'Sends book text to the configured remote provider. This choice is not saved and applies only to this book tab.',
            cloudActive: 'Cloud translation is active: book text is sent to the configured remote primary provider.',
            cloudSecondary: 'Allow secondary remote fallback',
            cloudSecondaryPrivacy: 'The primary provider is already remote. This additionally permits the configured remote fallback for this tab.',
            barPos: 'Position', posTop: 'Top', posBottom: 'Bottom', posReset: 'Reset to bottom', posDragHint: 'Touch or drag the bar anywhere to move it.',
            dbgQueue: 'Queue', dbgGen: 'Generation', dbgTrigger: 'Last trigger',
            stylePreset: 'Text style', presetDefault: 'Default', presetContrast: 'High contrast', presetLarge: 'Large text',
            glossary: 'Glossary', glossaryAdd: 'Add', glossarySource: 'Term', glossaryTarget: 'Translation',
            glossaryEmpty: 'No glossary terms for this book yet.',
            glossaryHint: 'Exact terms always translated the same way in this book.',
            glossaryDelete: 'Remove term',
            exportEpub: 'Export translated EPUB',
            exportEmpty: 'Nothing translated yet — nothing to export.',
            exportFailed: 'EPUB export failed.',
            exportDone: 'EPUB downloaded.',
            ttsSpeak: 'Listen',
            ttsPause: 'Pause',
            ttsResume: 'Resume',
            ttsStop: 'Stop',
            ttsEmpty: 'Nothing to read yet.',
            ttsUnsupported: 'Speech is not supported in this browser.',
            fbUp: 'Good translation',
            fbDown: 'Bad translation',
            fbThanks: 'Thanks for the feedback.',
            offline: 'Offline — resumes on reconnect',
        },
        es: {
            off: 'Original', bilingual: 'Bilingüe', translated: 'Traducido',
            translatingPage: 'Traduciendo…', translatingChapter: 'Capítulo', done: '✓ Listo', error: '⚠ Reintentar',
            rateLimited: 'Esperando {n}s…',
            retrying: 'Reintentando…',
            cycleHint: 'Clic para cambiar: Original → Bilingüe → Traducido', langHint: 'Idioma destino', sourceLangHint: 'Idioma fuente', topLanguages: 'Más hablados', allLanguages: 'Todos los idiomas (A–Z)', settings: 'Ajustes',
            prefetchWhole: 'Lectura fluida (precarga anticipada)', clearLang: 'Borrar caché de este idioma', clearAll: 'Borrar toda la caché',
            cached: 'En caché', cleared: 'Caché borrada',
            bookTranslator: 'Book Translator', modeLabel: 'Modo', sourceLabel: 'Fuente', targetLabel: 'Destino', langLabel: 'Idioma',
            retryPage: 'Reintentar página actual', debug: 'Depuración',
            cloudFallback: 'Permitir fallback cloud',
            cloudPrivacy: 'Envía texto del libro al proveedor remoto configurado. Esta elección no se guarda y solo aplica a esta pestaña del libro.',
            cloudActive: 'La traducción cloud está activa: el texto se envía al proveedor primario remoto configurado.',
            cloudSecondary: 'Permitir fallback remoto secundario',
            cloudSecondaryPrivacy: 'El proveedor primario ya es remoto. Esto además permite el fallback remoto configurado durante esta pestaña.',
            barPos: 'Posición', posTop: 'Arriba', posBottom: 'Abajo', posReset: 'Restablecer abajo', posDragHint: 'Arrastra la barra para moverla libremente.',
            dbgQueue: 'Cola', dbgGen: 'Generación', dbgTrigger: 'Último disparo',
            restoring: 'Restaurando traducciones guardadas…',
            stylePreset: 'Estilo de texto', presetDefault: 'Predeterminado', presetContrast: 'Alto contraste', presetLarge: 'Texto grande',
        },
        fr: {
            off: 'Original', bilingual: 'Bilingue', translated: 'Traduit',
            translatingPage: 'Traduction…', translatingChapter: 'Chapitre', done: '✓ Terminé', error: '⚠ Réessayer',
            rateLimited: 'Attente {n}s…',
            retrying: 'Nouvelle tentative…',
            restoring: 'Restauration des traductions enregistrées…',
            cycleHint: 'Cliquez pour changer : Original → Bilingue → Traduit', langHint: 'Langue cible', sourceLangHint: 'Langue source', topLanguages: 'Les plus parlées', allLanguages: 'Toutes les langues (A–Z)', settings: 'Réglages',
            prefetchWhole: 'Pré-traduire tout le chapitre', clearLang: 'Vider le cache de cette langue', clearAll: 'Vider tout le cache',
            cached: 'En cache', cleared: 'Cache vidé',
            bookTranslator: 'Traducteur de livres', modeLabel: 'Mode', sourceLabel: 'Source', targetLabel: 'Cible', langLabel: 'Langue',
            retryPage: 'Réessayer la page actuelle', debug: 'Débogage',
            cloudFallback: 'Autoriser le fallback cloud',
            cloudPrivacy: 'Envoie le texte du livre au fournisseur distant configuré. Ce choix n’est pas enregistré et s’applique uniquement à cet onglet du livre.',
            cloudActive: 'Traduction cloud active : le texte du livre est envoyé au fournisseur distant principal configuré.',
            cloudSecondary: 'Autoriser le fallback distant secondaire',
            cloudSecondaryPrivacy: 'Le fournisseur principal est déjà distant. Cela autorise en plus le fallback distant configuré pendant cet onglet.',
            barPos: 'Position', posTop: 'Haut', posBottom: 'Bas', posReset: 'Réinitialiser en bas', posDragHint: 'Touchez ou faites glisser la barre pour la déplacer.',
            dbgQueue: 'File', dbgGen: 'Génération', dbgTrigger: 'Dernier déclenchement',
            stylePreset: 'Style de texte', presetDefault: 'Par défaut', presetContrast: 'Contraste élevé', presetLarge: 'Grand texte',
        },
        de: {
            off: 'Original', bilingual: 'Zweisprachig', translated: 'Übersetzt',
            translatingPage: 'Übersetzen…', translatingChapter: 'Kapitel', done: '✓ Fertig', error: '⚠ Erneut',
            rateLimited: 'Warten {n}s…',
            retrying: 'Wiederholen…',
            restoring: 'Gespeicherte Übersetzungen werden wiederhergestellt…',
            cycleHint: 'Klicken zum Wechseln: Original → Zweisprachig → Übersetzt', langHint: 'Zielsprache', sourceLangHint: 'Ausgangssprache', topLanguages: 'Meistgesprochen', allLanguages: 'Alle Sprachen (A–Z)', settings: 'Einstellungen',
            prefetchWhole: 'Ganzes Kapitel vorübersetzen', clearLang: 'Cache dieser Sprache leeren', clearAll: 'Gesamten Cache leeren',
            cached: 'Im Cache', cleared: 'Cache geleert',
            bookTranslator: 'Buchübersetzer', modeLabel: 'Modus', sourceLabel: 'Quelle', targetLabel: 'Ziel', langLabel: 'Sprache',
            retryPage: 'Aktuelle Seite erneut versuchen', debug: 'Debug',
            cloudFallback: 'Cloud-Fallback erlauben',
            cloudPrivacy: 'Sendet Buchtext an den konfigurierten Remote-Anbieter. Diese Auswahl wird nicht gespeichert und gilt nur für diesen Buch-Tab.',
            cloudActive: 'Cloud-Übersetzung ist aktiv: Buchtext wird an den konfigurierten primären Remote-Anbieter gesendet.',
            cloudSecondary: 'Sekundären Remote-Fallback erlauben',
            cloudSecondaryPrivacy: 'Der primäre Anbieter ist bereits remote. Dies erlaubt zusätzlich den konfigurierten Remote-Fallback für diesen Tab.',
            barPos: 'Position', posTop: 'Oben', posBottom: 'Unten', posReset: 'Nach unten zurücksetzen', posDragHint: 'Leiste berühren oder ziehen, um sie zu verschieben.',
            dbgQueue: 'Warteschlange', dbgGen: 'Generation', dbgTrigger: 'Letzter Auslöser',
            stylePreset: 'Textstil', presetDefault: 'Standard', presetContrast: 'Hoher Kontrast', presetLarge: 'Großer Text',
        },
        pt: {
            off: 'Original', bilingual: 'Bilíngue', translated: 'Traduzido',
            translatingPage: 'Traduzindo…', translatingChapter: 'Capítulo', done: '✓ Pronto', error: '⚠ Repetir',
            rateLimited: 'Aguardando {n}s…',
            retrying: 'Tentando novamente…',
            restoring: 'Restaurando traduções salvas…',
            cycleHint: 'Clique para alternar: Original → Bilíngue → Traduzido', langHint: 'Idioma de destino', sourceLangHint: 'Idioma de origem', topLanguages: 'Mais falados', allLanguages: 'Todos os idiomas (A–Z)', settings: 'Ajustes',
            prefetchWhole: 'Pré-traduzir capítulo inteiro', clearLang: 'Limpar cache deste idioma', clearAll: 'Limpar todo o cache',
            cached: 'Em cache', cleared: 'Cache limpo',
            bookTranslator: 'Tradutor de livros', modeLabel: 'Modo', sourceLabel: 'Origem', targetLabel: 'Destino', langLabel: 'Idioma',
            retryPage: 'Repetir página atual', debug: 'Depuração',
            cloudFallback: 'Permitir fallback cloud',
            cloudPrivacy: 'Envia o texto do livro ao provedor remoto configurado. Esta escolha não é salva e aplica-se apenas a esta aba do livro.',
            cloudActive: 'Tradução cloud ativa: o texto do livro é enviado ao provedor remoto principal configurado.',
            cloudSecondary: 'Permitir fallback remoto secundário',
            cloudSecondaryPrivacy: 'O provedor principal já é remoto. Isto permite adicionalmente o fallback remoto configurado durante esta aba.',
            barPos: 'Posição', posTop: 'Topo', posBottom: 'Base', posReset: 'Repor na base', posDragHint: 'Toque ou arraste a barra para movê-la.',
            dbgQueue: 'Fila', dbgGen: 'Geração', dbgTrigger: 'Último disparo',
            stylePreset: 'Estilo de texto', presetDefault: 'Padrão', presetContrast: 'Alto contraste', presetLarge: 'Texto grande',
        },
    };
    // English is the base; the locale (if any) overrides it, so menu-only keys
    // added only to `en` never come out undefined in another language.
    const t = Object.assign({}, strings.en, strings[browserCode] || {});

    // Language catalog. `code` is the English language name sent to the API
    // (and used as the cache key); `name` is the endonym shown in the picker.
    // The set mirrors the languages Gemma 4 (the default backend model) was
    // pre-trained on; the top-10 most spoken languages get their own group,
    // the rest are alphabetical. Native <select> gives type-to-search.
    // NOTE: must stay in sync with VALID_LANGUAGES in server.py (a test
    // asserts this).
    const TOP_LANGUAGES = [
        { code: 'English', name: 'English' },
        { code: 'Chinese', name: '中文' },
        { code: 'Hindi', name: 'हिन्दी' },
        { code: 'Spanish', name: 'Español' },
        { code: 'French', name: 'Français' },
        { code: 'Arabic', name: 'العربية' },
        { code: 'Bengali', name: 'বাংলা' },
        { code: 'Portuguese', name: 'Português' },
        { code: 'Russian', name: 'Русский' },
        { code: 'Urdu', name: 'اردو' }
    ];

    const MORE_LANGUAGES = [
        { code: 'Afrikaans', name: 'Afrikaans' },
        { code: 'Albanian', name: 'Shqip' },
        { code: 'Amharic', name: 'አማርኛ' },
        { code: 'Aymara', name: 'Aymar aru' },
        { code: 'Basque', name: 'Euskara' },
        { code: 'Bosnian', name: 'Bosanski' },
        { code: 'Bulgarian', name: 'Български' },
        { code: 'Burmese', name: 'မြန်မာ' },
        { code: 'Catalan', name: 'Català' },
        { code: 'Cebuano', name: 'Cebuano' },
        { code: 'Chewa', name: 'Chichewa' },
        { code: 'Chinese (Traditional)', name: '中文（繁體）' },
        { code: 'Croatian', name: 'Hrvatski' },
        { code: 'Czech', name: 'Čeština' },
        { code: 'Danish', name: 'Dansk' },
        { code: 'Dutch', name: 'Nederlands' },
        { code: 'Esperanto', name: 'Esperanto' },
        { code: 'Estonian', name: 'Eesti' },
        { code: 'Finnish', name: 'Suomi' },
        { code: 'Gaelic', name: 'Gàidhlig' },
        { code: 'Galician', name: 'Galego' },
        { code: 'Ganda', name: 'Luganda' },
        { code: 'German', name: 'Deutsch' },
        { code: 'Greek', name: 'Ελληνικά' },
        { code: 'Guarani', name: 'Avañe\'ẽ' },
        { code: 'Gujarati', name: 'ગુજરાતી' },
        { code: 'Hausa', name: 'Hausa' },
        { code: 'Hawaiian', name: 'ʻŌlelo Hawaiʻi' },
        { code: 'Hebrew', name: 'עברית' },
        { code: 'Hungarian', name: 'Magyar' },
        { code: 'Icelandic', name: 'Íslenska' },
        { code: 'Igbo', name: 'Igbo' },
        { code: 'Indonesian', name: 'Bahasa Indonesia' },
        { code: 'Italian', name: 'Italiano' },
        { code: 'Japanese', name: '日本語' },
        { code: 'Javanese', name: 'Basa Jawa' },
        { code: 'Kannada', name: 'ಕನ್ನಡ' },
        { code: 'Kazakh', name: 'Қазақша' },
        { code: 'Khmer', name: 'ខ្មែរ' },
        { code: 'Korean', name: '한국어' },
        { code: 'Kyrgyz', name: 'Кыргызча' },
        { code: 'Lao', name: 'ລາວ' },
        { code: 'Latin', name: 'Latina' },
        { code: 'Latvian', name: 'Latviešu' },
        { code: 'Lingala', name: 'Lingála' },
        { code: 'Lithuanian', name: 'Lietuvių' },
        { code: 'Macedonian', name: 'Македонски' },
        { code: 'Maithili', name: 'मैथिली' },
        { code: 'Malagasy', name: 'Malagasy' },
        { code: 'Malay', name: 'Bahasa Melayu' },
        { code: 'Malayalam', name: 'മലയാളം' },
        { code: 'Maori', name: 'Te Reo Māori' },
        { code: 'Marathi', name: 'मराठी' },
        { code: 'Mongolian', name: 'Монгол' },
        { code: 'Nahuatl', name: 'Nāhuatl' },
        { code: 'Navajo', name: 'Diné bizaad' },
        { code: 'Nepali', name: 'नेपाली' },
        { code: 'Norwegian', name: 'Norsk' },
        { code: 'Odia', name: 'ଓଡ଼ିଆ' },
        { code: 'Oromo', name: 'Afaan Oromoo' },
        { code: 'Pashto', name: 'پښتو' },
        { code: 'Persian', name: 'فارسی' },
        { code: 'Polish', name: 'Polski' },
        { code: 'Punjabi', name: 'ਪੰਜਾਬੀ' },
        { code: 'Quechua', name: 'Runa Simi' },
        { code: 'Romanian', name: 'Română' },
        { code: 'Samoan', name: 'Gagana Samoa' },
        { code: 'Serbian', name: 'Српски' },
        { code: 'Shona', name: 'chiShona' },
        { code: 'Sindhi', name: 'سنڌي' },
        { code: 'Sinhala', name: 'සිංහල' },
        { code: 'Slovak', name: 'Slovenčina' },
        { code: 'Slovenian', name: 'Slovenščina' },
        { code: 'Somali', name: 'Soomaali' },
        { code: 'Sundanese', name: 'Basa Sunda' },
        { code: 'Swahili', name: 'Kiswahili' },
        { code: 'Swedish', name: 'Svenska' },
        { code: 'Tagalog', name: 'Tagalog' },
        { code: 'Tajik', name: 'Тоҷикӣ' },
        { code: 'Tamil', name: 'தமிழ்' },
        { code: 'Telugu', name: 'తెలుగు' },
        { code: 'Thai', name: 'ไทย' },
        { code: 'Tibetan', name: 'བོད་སྐད' },
        { code: 'Turkish', name: 'Türkçe' },
        { code: 'Turkmen', name: 'Türkmençe' },
        { code: 'Ukrainian', name: 'Українська' },
        { code: 'Uzbek', name: 'Oʻzbekcha' },
        { code: 'Vietnamese', name: 'Tiếng Việt' },
        { code: 'Welsh', name: 'Cymraeg' },
        { code: 'Xhosa', name: 'isiXhosa' },
        { code: 'Yoruba', name: 'Yorùbá' },
        { code: 'Zulu', name: 'isiZulu' }
    ];

    const availableLangs = TOP_LANGUAGES.concat(MORE_LANGUAGES);
    const availableLangCodes = new Set(availableLangs.map(language => language.code));
    if (!availableLangCodes.has(SOURCE_LANG)) SOURCE_LANG = 'English';
    if (!availableLangCodes.has(TARGET_LANG)) TARGET_LANG = defaultLang;

    function sourceLanguageOptions(selected, detected) {
        const autoLabel = detected
            ? `${t.autoDetect || 'Auto (detect)'} — ${detected}`
            : (t.autoDetect || 'Auto (detect)');
        const autoSelected = (selected === 'Auto' || !selected) ? ' selected' : '';
        const autoOption = `<option value="Auto"${autoSelected}>${autoLabel}</option>`;
        return `<optgroup label="Auto">${autoOption}</optgroup>` +
            `<optgroup label="${t.topLanguages}">${TOP_LANGUAGES.map(l => `<option value="${l.code}"${l.code === selected ? ' selected' : ''}>${l.name === l.code ? l.code : `${l.name} — ${l.code}`}</option>`).join('')}</optgroup>` +
            `<optgroup label="${t.allLanguages}">${MORE_LANGUAGES.map(l => `<option value="${l.code}"${l.code === selected ? ' selected' : ''}>${l.code} — ${l.name}</option>`).join('')}</optgroup>`;
    }

    function languageOptions(selected) {
        const option = (language, englishFirst) => {
            const label = language.name === language.code ? language.code
                : englishFirst
                    ? `${language.code} — ${language.name}`
                    : `${language.name} — ${language.code}`;
            return `<option value="${language.code}"${language.code === selected ? ' selected' : ''}>${label}</option>`;
        };
        return (
            `<optgroup label="${t.topLanguages}">${TOP_LANGUAGES.map(language => option(language, false)).join('')}</optgroup>` +
            `<optgroup label="${t.allLanguages}">${MORE_LANGUAGES.map(language => option(language, true)).join('')}</optgroup>`
        );
    }

    // ── UI Components ──────────────────────────────────────────────────
    function setMode(mode, { silent = false } = {}) {
        const prevMode = translationMode;
        if (mode === prevMode) return;
        translationMode = mode;
        localStorage.setItem('bt_mode', mode);
        bookPrefRemember('bt_mode', mode);

        const bar = document.getElementById('bt-bar');
        if (bar) bar.dataset.mode = mode;
        const toggle = document.getElementById('bt-toggle-label');
        if (toggle) toggle.textContent = mode === 'bilingual' ? t.bilingual
            : mode === 'translated' ? t.translated : t.off;

        if (mode === 'off') {
            newGeneration();              // cancel in-flight work; next ON starts clean
            removeAllTranslations();
            refreshStatus();
            if (!silent) showToast(t.off);
        } else if (prevMode === 'off') {
            translateCurrentPage();       // fresh start
        } else {
            // bilingual <-> translated: re-render from cache instantly, keep filling gaps
            renderMode(getParagraphs());
            translateCurrentPage();
        }
    }

    // ── Draggable Floating Bar & Position Persistence ────────────────────────
    let isDraggingBar = false;
    let dragStartX = 0, dragStartY = 0;
    let barStartX = 0, barStartY = 0;
    let hasMovedDuringDrag = false;

    function applyBarPosition() {
        const bar = document.getElementById('bt-bar');
        if (!bar) return;
        const raw = localStorage.getItem('bt_pos');
        if (!raw || raw === 'bottom') {
            bar.style.top = 'auto';
            bar.style.bottom = '22px';
            bar.style.left = '50%';
            bar.style.transform = 'translateX(-50%)';
            bar.style.cursor = 'default';
        } else if (raw === 'top') {
            bar.style.bottom = 'auto';
            bar.style.top = '22px';
            bar.style.left = '50%';
            bar.style.transform = 'translateX(-50%)';
            bar.style.cursor = 'default';
        } else {
            try {
                const pos = JSON.parse(raw);
                if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
                    const maxX = Math.max(10, window.innerWidth - (bar.offsetWidth || 280) - 10);
                    const maxY = Math.max(10, window.innerHeight - (bar.offsetHeight || 44) - 10);
                    const x = Math.max(10, Math.min(maxX, pos.x));
                    const y = Math.max(10, Math.min(maxY, pos.y));
                    bar.style.bottom = 'auto';
                    bar.style.left = x + 'px';
                    bar.style.top = y + 'px';
                    bar.style.transform = 'none';
                    bar.style.cursor = 'move';
                }
            } catch (e) {
                localStorage.removeItem('bt_pos');
            }
        }
        updateMenuPosition();
    }

    function setBarPresetPosition(posName) {
        if (posName === 'custom') return;
        localStorage.setItem('bt_pos', posName);
        applyBarPosition();
        buildMenu();
        closeMenu({ restoreFocus: false });
        showToast(posName === 'top' ? (t.posTop || 'Arriba') : (t.posBottom || 'Abajo'));
    }

    function updateMenuPosition() {
        const bar = document.getElementById('bt-bar');
        const menu = document.getElementById('bt-menu');
        if (!bar || !menu) return;
        const rect = bar.getBoundingClientRect();
        const menuWidth = menu.offsetWidth || 280;
        const menuHeight = menu.offsetHeight || 300;

        let left = rect.left + (rect.width / 2) - (menuWidth / 2);
        left = Math.max(10, Math.min(window.innerWidth - menuWidth - 10, left));
        menu.style.left = left + 'px';
        menu.style.transform = 'none';

        if (rect.top > menuHeight + 30) {
            menu.style.top = 'auto';
            menu.style.bottom = (window.innerHeight - rect.top + 10) + 'px';
        } else {
            menu.style.bottom = 'auto';
            menu.style.top = Math.min(window.innerHeight - 80, rect.bottom + 10) + 'px';
        }
    }

    function setupBarDragEvents(bar) {
        function onPointerDown(e) {
            if (e.target.closest('#bt-lang, #bt-gear, select')) return;
            isDraggingBar = true;
            hasMovedDuringDrag = false;
            dragStartX = e.clientX || (e.touches && e.touches[0].clientX) || 0;
            dragStartY = e.clientY || (e.touches && e.touches[0].clientY) || 0;
            const rect = bar.getBoundingClientRect();
            barStartX = rect.left;
            barStartY = rect.top;

            document.addEventListener('pointermove', onPointerMove, { passive: false });
            document.addEventListener('touchmove', onPointerMove, { passive: false });
            document.addEventListener('pointerup', onPointerUp);
            document.addEventListener('touchend', onPointerUp);
        }

        function onPointerMove(e) {
            if (!isDraggingBar) return;
            const cx = e.clientX || (e.touches && e.touches[0].clientX) || 0;
            const cy = e.clientY || (e.touches && e.touches[0].clientY) || 0;
            const dx = cx - dragStartX;
            const dy = cy - dragStartY;

            if (Math.hypot(dx, dy) > 5) {
                hasMovedDuringDrag = true;
                if (e.cancelable) e.preventDefault();
                const maxX = Math.max(10, window.innerWidth - bar.offsetWidth - 10);
                const maxY = Math.max(10, window.innerHeight - bar.offsetHeight - 10);
                const newX = Math.max(10, Math.min(maxX, barStartX + dx));
                const newY = Math.max(10, Math.min(maxY, barStartY + dy));

                bar.style.bottom = 'auto';
                bar.style.left = newX + 'px';
                bar.style.top = newY + 'px';
                bar.style.transform = 'none';
                bar.style.cursor = 'move';
            }
        }

        function onPointerUp(e) {
            if (!isDraggingBar) return;
            isDraggingBar = false;
            document.removeEventListener('pointermove', onPointerMove);
            document.removeEventListener('touchmove', onPointerMove);
            document.removeEventListener('pointerup', onPointerUp);
            document.removeEventListener('touchend', onPointerUp);

            if (hasMovedDuringDrag) {
                const rect = bar.getBoundingClientRect();
                localStorage.setItem('bt_pos', JSON.stringify({ x: Math.round(rect.left), y: Math.round(rect.top) }));
                updateMenuPosition();
            }
        }

        bar.addEventListener('pointerdown', onPointerDown);
        bar.addEventListener('touchstart', onPointerDown, { passive: true });
        window.addEventListener('resize', applyBarPosition);
    }

    function createFloatingUI() {
        if (document.getElementById('bt-bar')) return;

        const bar = document.createElement('div');
        bar.id = 'bt-bar';
        bar.dataset.mode = translationMode;
        bar.dataset.state = 'idle';
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', t.bookTranslator);
        bar.setAttribute('dir', 'auto');
        bar.dataset.preset = stylePreset;

        // Build the language <option> list once: top-10 most spoken first,
        // then every other supported language A-Z.
        // Label format matters for usability:
        //  - top-10: "Endonym — English" (endonym is the recognizable form there)
        //  - A-Z group: "English — Endonym", so the VISIBLE text is what the list
        //    is sorted by (endonym-first looked unsorted) and the native select's
        //    type-to-jump works with a latin keyboard for every language.
        const langOptions = languageOptions(TARGET_LANG);

        bar.innerHTML =
            `<button type="button" id="bt-toggle" title="${t.cycleHint}">` +
                `<span class="bt-dot"></span>` +
                `<span id="bt-toggle-label">${translationMode === 'bilingual' ? t.bilingual : translationMode === 'translated' ? t.translated : t.off}</span>` +
            `</button>` +
            `<span class="bt-target-arrow" title="${t.targetLabel || 'Destino'}" aria-hidden="true">→</span>` +
            `<select id="bt-lang" title="${t.targetLabel || 'Destino'}: ${t.langHint}" aria-label="${t.targetLabel || 'Destino'}">${langOptions}</select>` +
            `<div id="bt-status" role="status" aria-live="polite" aria-atomic="true">` +
                `<span id="bt-spinner"></span>` +
                `<span id="bt-status-text"></span>` +
            `</div>` +
            `<button type="button" id="bt-gear" title="${t.settings}" aria-label="${t.settings}" aria-haspopup="dialog" aria-controls="bt-menu" aria-expanded="false">⚙</button>` +
            `<div id="bt-progress" role="progressbar" aria-label="${t.translatingChapter}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="bt-progress-fill"></div></div>`;

        document.body.appendChild(bar);
        bar.ondblclick = (e) => {
            if (e.target.closest('#bt-lang, #bt-gear, select, button')) return;
            setBarPresetPosition('bottom');
        };
        setupBarDragEvents(bar);

        // The settings popover lives at body level (NOT inside #bt-bar) because the
        // bar uses overflow:hidden to clip the progress bar, which would also clip
        // a child popover — that's why the gear "did nothing" before.
        const menu = document.createElement('div');
        menu.id = 'bt-menu';
        menu.setAttribute('role', 'dialog');
        menu.setAttribute('aria-label', t.settings);
        menu.setAttribute('aria-hidden', 'true');
        menu.setAttribute('tabindex', '-1');
        menu.setAttribute('dir', 'auto');
        document.body.appendChild(menu);
        setStylePreset(stylePreset);

        document.getElementById('bt-toggle').onclick = () => {
            if (hasMovedDuringDrag) { hasMovedDuringDrag = false; return; }
            const next = translationMode === 'off' ? 'bilingual'
                : translationMode === 'bilingual' ? 'translated' : 'off';
            setMode(next);
        };

        function setTargetLanguage(newLang) {
            if (!availableLangCodes.has(newLang) || newLang === TARGET_LANG) return;
            persistCacheNow();
            TARGET_LANG = newLang;
            localStorage.setItem('bt_lang', TARGET_LANG);
            bookPrefRemember('bt_lang', TARGET_LANG);

            const barSel = document.getElementById('bt-lang');
            if (barSel && barSel.value !== TARGET_LANG) barSel.value = TARGET_LANG;
            const menuSel = document.getElementById('bt-menu-target-lang');
            if (menuSel && menuSel.value !== TARGET_LANG) menuSel.value = TARGET_LANG;

            newGeneration();
            translatedParagraphs = loadCacheForLang(TARGET_LANG);
            if (translationMode !== 'off') {
                removeAllTranslations();
                translateCurrentPage();
            }
            buildMenu();
            refreshStatus();
        }

        const sel = document.getElementById('bt-lang');
        sel.onchange = (e) => setTargetLanguage(e.target.value);

        const gear = document.getElementById('bt-gear');
        gear.onclick = (e) => { e.stopPropagation(); toggleMenu(); };


        // Close on outside click (anywhere not on the bar or the menu)...
        document.addEventListener('click', (e) => {
            if (menu.classList.contains('bt-open') && !bar.contains(e.target) && !menu.contains(e.target)) {
                closeMenu({ restoreFocus: false });
            }
        });
        // ...and on Escape.
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && menu.classList.contains('bt-open')) closeMenu();
        });

        const retryFailed = () => {
            if (bar.dataset.state === 'error') {
                errorCount = 0;
                failedParagraphs.clear();
                rateLimitResponses.clear();
                if (translationMode !== 'off') translateCurrentPage();
            }
        };
        const status = document.getElementById('bt-status');
        status.onclick = retryFailed;
        status.onkeydown = (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                if (bar.dataset.state === 'error') event.preventDefault();
                retryFailed();
            }
        };

        buildMenu();
        refreshStatus();
    }

    function buildMenu() {
        const menu = document.getElementById('bt-menu');
        if (!menu) return;
        const entryCount = Object.keys(translatedParagraphs).length;
        const modeLabel = t[translationMode] || translationMode;
        const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
        const escAttr = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
        const glossaryListHtml = glossaryEntries.length
            ? glossaryEntries.map((entry) =>
                `<div class="bt-gloss-row"><span class="bt-gloss-terms">${esc(entry.source)} → ${esc(entry.target)}</span>` +
                `<button type="button" class="bt-gloss-del" data-source="${escAttr(entry.source)}" title="${escAttr(t.glossaryDelete)}" aria-label="${escAttr(t.glossaryDelete)}">×</button></div>`).join('')
            : `<div class="bt-menu-note">${t.glossaryEmpty}</div>`;
        let privacyControls = '';
        if (providerPolicyState && providerPolicyState.primary === 'remote') {
            privacyControls +=
                `<div class="bt-menu-note bt-menu-warning" data-provider-policy="cloud-active">${t.cloudActive || strings.en.cloudActive}</div>`;
            if (providerPolicyState.fallback === 'remote') {
                privacyControls +=
                    `<button type="button" class="bt-menu-item" data-action="cloud-fallback" role="switch" aria-checked="${allowCloudFallback}">` +
                        `<span>${t.cloudSecondary || strings.en.cloudSecondary}</span>` +
                        `<span class="bt-switch${allowCloudFallback ? ' bt-on' : ''}" aria-hidden="true"></span>` +
                    `</button>` +
                    `<div class="bt-menu-note bt-menu-warning" data-provider-policy="remote-secondary">${t.cloudSecondaryPrivacy || strings.en.cloudSecondaryPrivacy}</div>`;
            }
        } else if (providerPolicyState
                && providerPolicyState.primary === 'local'
                && providerPolicyState.fallback === 'remote') {
            privacyControls =
                `<button type="button" class="bt-menu-item" data-action="cloud-fallback" role="switch" aria-checked="${allowCloudFallback}">` +
                    `<span>${t.cloudFallback || strings.en.cloudFallback}</span>` +
                    `<span class="bt-switch${allowCloudFallback ? ' bt-on' : ''}" aria-hidden="true"></span>` +
                `</button>` +
                `<div class="bt-menu-note bt-menu-warning" data-provider-policy="remote-fallback">${t.cloudPrivacy || strings.en.cloudPrivacy}</div>`;
        }
        const curPos = localStorage.getItem('bt_pos') || 'bottom';
        const isTop = curPos === 'top';
        const isCustom = curPos !== 'bottom' && curPos !== 'top';
        const posLabel = isTop ? (t.posTop || 'Arriba') : isCustom ? 'Libre (arrastrada)' : (t.posBottom || 'Abajo');

        menu.innerHTML =
            `<div class="bt-menu-header">${t.bookTranslator}<span class="bt-menu-ver">v${BT_UI_VERSION}</span></div>` +
            `<div class="bt-menu-row"><span>${t.modeLabel}</span><span class="bt-menu-val">${modeLabel}</span></div>` +
            `<label class="bt-menu-field" for="bt-source-lang"><span>${t.sourceLabel}</span>` +
                `<select id="bt-source-lang" class="bt-menu-select" title="${t.sourceLangHint}" aria-label="${t.sourceLangHint}">` +
                    `${sourceLanguageOptions(SOURCE_LANG_SETTING, detectedSourceLang)}</select></label>` +
            `<label class="bt-menu-field" for="bt-menu-target-lang"><span>${t.targetLabel}</span>` +
                `<select id="bt-menu-target-lang" class="bt-menu-select" title="${t.langHint}" aria-label="${t.langHint}">` +
                    `${languageOptions(TARGET_LANG)}</select></label>` +
            `<label class="bt-menu-field" for="bt-style-preset"><span>${t.stylePreset}</span>` +
                `<select id="bt-style-preset" class="bt-menu-select" aria-label="${t.stylePreset}">` +
                    `<option value="default"${stylePreset === 'default' ? ' selected' : ''}>${t.presetDefault}</option>` +
                    `<option value="contrast"${stylePreset === 'contrast' ? ' selected' : ''}>${t.presetContrast}</option>` +
                    `<option value="large"${stylePreset === 'large' ? ' selected' : ''}>${t.presetLarge}</option>` +
                `</select></label>` +
            `<div class="bt-menu-row"><span>${t.barPos || 'Posición'}</span><span class="bt-menu-val">${posLabel}</span></div>` +
            `<div class="bt-menu-sep"></div>` +
            `<button type="button" class="bt-menu-item" data-action="toggle-pos">` +
                `<span>↕ Mover a ${isTop ? (t.posBottom || 'Abajo') : (t.posTop || 'Arriba')}</span>` +
            `</button>` +
            `<button type="button" class="bt-menu-item" data-action="reset-pos">` +
                `<span>↺ ${t.posReset || 'Restablecer abajo'}</span>` +
            `</button>` +
            `<div class="bt-menu-sep"></div>` +
            `<button type="button" class="bt-menu-item" data-action="prefetch" role="switch" aria-checked="${prefetchEnabled}">` +
                `<span>${t.prefetchWhole}</span>` +
                `<span class="bt-switch${prefetchEnabled ? ' bt-on' : ''}" aria-hidden="true"></span>` +
            `</button>` +
            privacyControls +
            `<button type="button" class="bt-menu-item" data-action="retry"><span>↻ ${t.retryPage}</span></button>` +
            `<button type="button" class="bt-menu-item" data-action="export-epub"><span>⤓ ${t.exportEpub}</span></button>` +
            `<button type="button" class="bt-menu-item" data-action="clear-lang"><span>${t.clearLang}</span></button>` +
            `<button type="button" class="bt-menu-item" data-action="clear-all"><span>${t.clearAll}</span></button>` +
            `<div class="bt-menu-sep"></div>` +
            `<div class="bt-menu-row"><span>${t.glossary}</span><span class="bt-menu-val">${glossaryEntries.length}</span></div>` +
            `<div id="bt-gloss-list">${glossaryListHtml}</div>` +
            `<form id="bt-gloss-form" class="bt-gloss-form">` +
                `<input id="bt-gloss-source" class="bt-gloss-input" maxlength="200" placeholder="${escAttr(t.glossarySource)}" aria-label="${escAttr(t.glossarySource)}" autocomplete="off">` +
                `<input id="bt-gloss-target" class="bt-gloss-input" maxlength="200" placeholder="${escAttr(t.glossaryTarget)}" aria-label="${escAttr(t.glossaryTarget)}" autocomplete="off">` +
                `<button type="submit" class="bt-gloss-add">${t.glossaryAdd}</button>` +
            `</form>` +
            `<div class="bt-menu-note">${t.glossaryHint}</div>` +
            `<div class="bt-menu-sep"></div>` +
            `<div class="bt-menu-note">💡 ${t.posDragHint || 'Arrastra la barra para moverla libremente.'}</div>` +
            `<div class="bt-menu-note">${t.cached}: ${entryCount} · ${esc(TARGET_LANG)}</div>` +
            `<div class="bt-menu-note">${t.debug}: ${t.dbgQueue} ${prefetchQueue.length} · ${t.dbgGen} ${generation} · ${t.dbgTrigger} ${esc(lastTriggerReason)}</div>`;

        const presetSelect = menu.querySelector('#bt-style-preset');
        if (presetSelect) {
            presetSelect.onchange = (event) => {
                setStylePreset(event.target.value);
                buildMenu();
            };
        }

        const glossForm = menu.querySelector('#bt-gloss-form');
        if (glossForm) {
            glossForm.onsubmit = (event) => {
                event.preventDefault();
                const source = menu.querySelector('#bt-gloss-source').value.trim();
                const target = menu.querySelector('#bt-gloss-target').value.trim();
                if (!source || !target) return;
                saveGlossaryTerm(source, target).then(() => buildMenu());
            };
        }
        menu.querySelectorAll('.bt-gloss-del').forEach((btn) => {
            btn.onclick = (e) => {
                e.stopPropagation();
                deleteGlossaryTerm(btn.dataset.source).then(() => buildMenu());
            };
        });

        const sourceSelect = menu.querySelector('#bt-source-lang');
        if (sourceSelect) {
            sourceSelect.onchange = (event) => {
                persistCacheNow();
                SOURCE_LANG_SETTING = event.target.value;
                localStorage.setItem('bt_source_lang', SOURCE_LANG_SETTING);
                bookPrefRemember('bt_source_lang', SOURCE_LANG_SETTING);
                SOURCE_LANG = resolveEffectiveSourceLang(getReaderDoc());
                newGeneration();
                translatedParagraphs = loadCacheForLang(TARGET_LANG);
                removeAllTranslations();
                if (translationMode !== 'off') translateCurrentPage();
                buildMenu();
                refreshStatus();
            };
        }

        const targetSelect = menu.querySelector('#bt-menu-target-lang');
        if (targetSelect) {
            targetSelect.onchange = (event) => {
                setTargetLanguage(event.target.value);
            };
        }

        menu.querySelectorAll('.bt-menu-item').forEach(item => {
            item.onclick = (e) => {
                e.stopPropagation();
                const action = item.dataset.action;
                if (action === 'toggle-pos') {
                    const nextPos = (localStorage.getItem('bt_pos') === 'top') ? 'bottom' : 'top';
                    setBarPresetPosition(nextPos);
                } else if (action === 'reset-pos') {
                    setBarPresetPosition('bottom');
                } else if (action === 'prefetch') {
                    prefetchEnabled = !prefetchEnabled;
                    localStorage.setItem('bt_prefetch', prefetchEnabled ? '1' : '0');
                    buildMenu();
                    if (!prefetchEnabled) {
                        prefetchQueue = [];
                        refreshStatus();
                    } else if (translationMode !== 'off') {
                        scheduleTranslate('prefetch_enabled', { immediate: true, forceRediscover: true });
                    }
                } else if (action === 'cloud-fallback') {
                    allowCloudFallback = !allowCloudFallback;
                    buildMenu();
                } else if (action === 'retry') {
                    errorCount = 0;
                    failedParagraphs.clear();
                    closeMenu();
                    if (translationMode !== 'off') scheduleTranslate('manual_retry', { immediate: true, forceRediscover: true });
                } else if (action === 'export-epub') {
                    closeMenu({ restoreFocus: false });
                    exportTranslatedEpub();
                } else if (action === 'clear-lang') {
                    translatedParagraphs = {};
                    failedParagraphs.clear();
                    rateLimitResponses.clear();
                    try { localStorage.removeItem(CACHE_PREFIX + TARGET_LANG); } catch (e2) {}
                    showToast(t.cleared);
                    buildMenu();
                } else if (action === 'clear-all') {
                    translatedParagraphs = {};
                    failedParagraphs.clear();
                    rateLimitResponses.clear();
                    try {
                        Object.keys(localStorage).filter(k => k.startsWith(CACHE_PREFIX))
                            .forEach(k => localStorage.removeItem(k));
                    } catch (e2) {}
                    showToast(t.cleared);
                    buildMenu();
                }
            };
        });
    }

    function closeMenu({ restoreFocus = true } = {}) {
        const menu = document.getElementById('bt-menu');
        const gear = document.getElementById('bt-gear');
        const wasOpen = !!(menu && menu.classList.contains('bt-open'));
        if (menu) {
            menu.classList.remove('bt-open');
            menu.setAttribute('aria-hidden', 'true');
        }
        if (gear) gear.setAttribute('aria-expanded', 'false');
        if (restoreFocus && wasOpen && gear) gear.focus();
    }

    function toggleMenu() {
        const menu = document.getElementById('bt-menu');
        if (!menu) return;
        if (!menu.classList.contains('bt-open')) {
            buildMenu(); // refresh snapshot (mode/queue/gen)
            updateMenuPosition();
        }
        menu.classList.toggle('bt-open');
        if (menu.classList.contains('bt-open')) updateMenuPosition();
        const isOpen = menu.classList.contains('bt-open');
        if (isOpen) {
            // Lazy glossary load: only on explicit menu open, never during
            // background menu rebuilds, so no fetch fires unless the user
            // opens settings. Rebuilds only when entries actually changed.
            const glossBefore = glossaryEntries;
            loadGlossary().then(() => {
                if (glossaryEntries !== glossBefore) buildMenu();
            });
        }
        menu.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
        const gear = document.getElementById('bt-gear');
        if (gear) gear.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        if (isOpen) {
            const target = menu.querySelector('select, button, [tabindex="0"]') || menu;
            window.requestAnimationFrame(() => target.focus());
        } else if (gear) {
            gear.focus();
        }
    }

    // Single source of truth for the status zone: derives display from state.
    function refreshStatus() {
        if (typeof document === 'undefined') return;
        const bar = document.getElementById('bt-bar');
        const text = document.getElementById('bt-status-text');
        const progress = document.getElementById('bt-progress');
        const status = document.getElementById('bt-status');
        const fill = document.getElementById('bt-progress-fill');
        if (!bar || !text) return;

        if (doneHideTimer) { clearTimeout(doneHideTimer); doneHideTimer = null; }

        let state = 'idle';
        let progressValue = 0;
        if (translationMode !== 'off') {
            const now = Date.now();
            if (isOffline) {
                state = 'offline';
                text.textContent = t.offline;
            } else if (rateLimitUntil > now) {
                state = 'ratelimit';
                const left = Math.ceil((rateLimitUntil - now) / 1000);
                text.textContent = (t.rateLimited || strings.en.rateLimited).replace('{n}', left);
                // Ensure UI updates countdown
                if (!window.btRateLimitTimer) {
                    window.btRateLimitTimer = setInterval(() => {
                        if (Date.now() > rateLimitUntil) { clearInterval(window.btRateLimitTimer); window.btRateLimitTimer = null; }
                        refreshStatus();
                    }, 1000);
                }
            } else if (errorCount > 0) {
                state = 'error';
                text.textContent = t.error;
            } else if (isTranslating) {
                state = 'page';
                // Visible-page work: show honest progress when a chapter
                // session is running (done counts BOTH visible and prefetch
                // paragraphs, so it never sits at 0 while translating).
                const p = chapterProgress();
                text.textContent = p.total > 1
                    ? `${t.translatingPage} ${Math.min(p.done, p.total)}/${p.total}`
                    : t.translatingPage;
            } else if (isPrefetching || prefetchQueue.length > 0) {
                state = 'chapter';
                const p = chapterProgress();
                const done = Math.min(p.done, p.total);
                progressValue = p.total > 0 ? Math.round(done / p.total * 100) : 0;
                if (fill) fill.style.width = progressValue + '%';
                text.textContent = `${t.translatingChapter} ${done}/${p.total}`;
            } else if (chapterDone > 0) {
                state = 'done';
                progressValue = 100;
                if (fill) fill.style.width = '100%';
                text.textContent = t.done;
                doneHideTimer = setTimeout(() => {
                    chapterDone = 0;
                    doneHideTimer = null;
                    refreshStatus();
                }, 2500);
            }
        }
        bar.dataset.state = state;
        if (status) {
            if (state === 'error') {
                status.setAttribute('role', 'button');
                status.setAttribute('tabindex', '0');
                status.setAttribute('aria-label', t.retryPage);
            } else {
                status.setAttribute('role', 'status');
                status.removeAttribute('tabindex');
                status.removeAttribute('aria-label');
            }
        }
        if (progress) {
            if (state === 'page') {
                progress.removeAttribute('aria-valuenow');
                progress.setAttribute('aria-valuetext', text.textContent || t.translatingPage);
            } else {
                progress.setAttribute('aria-valuenow', String(progressValue));
                progress.removeAttribute('aria-valuetext');
            }
        }
        if (fill && (state === 'idle' || state === 'page')) {
            // page state uses an indeterminate CSS animation; reset width otherwise
            if (state === 'idle') fill.style.width = '0%';
        }
    }

    // ── Toast Notifications ────────────────────────────────────────────
    function showToast(message) {
        let toast = document.getElementById('bt-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'bt-toast';
            toast.setAttribute('dir', 'auto');
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        requestAnimationFrame(() => toast.classList.add('bt-toast-visible'));
        clearTimeout(toast._btHide);
        toast._btHide = setTimeout(() => toast.classList.remove('bt-toast-visible'), 2600);
    }

    // ── Language Normalization & Auto-Detection ────────────────────────
    const ISO_TO_VALID_LANG = {
        'en': 'English', 'eng': 'English',
        'es': 'Spanish', 'spa': 'Spanish',
        'fr': 'French', 'fra': 'French', 'fre': 'French',
        'de': 'German', 'deu': 'German', 'ger': 'German',
        'it': 'Italian', 'ita': 'Italian',
        'pt': 'Portuguese', 'por': 'Portuguese',
        'ru': 'Russian', 'rus': 'Russian',
        'zh': 'Chinese', 'zho': 'Chinese', 'chi': 'Chinese',
        'ja': 'Japanese', 'jpn': 'Japanese',
        'ko': 'Korean', 'kor': 'Korean',
        'ar': 'Arabic', 'ara': 'Arabic',
        'hi': 'Hindi', 'hin': 'Hindi',
        'nl': 'Dutch', 'nld': 'Dutch', 'dut': 'Dutch',
        'pl': 'Polish', 'pol': 'Polish',
        'tr': 'Turkish', 'tur': 'Turkish',
        'uk': 'Ukrainian', 'ukr': 'Ukrainian',
        'cs': 'Czech', 'ces': 'Czech', 'cze': 'Czech',
        'sv': 'Swedish', 'swe': 'Swedish',
        'el': 'Greek', 'ell': 'Greek', 'gre': 'Greek',
        'ro': 'Romanian', 'ron': 'Romanian', 'rum': 'Romanian',
        'hu': 'Hungarian', 'hun': 'Hungarian',
        'da': 'Danish', 'dan': 'Danish',
        'fi': 'Finnish', 'fin': 'Finnish',
        'no': 'Norwegian', 'nor': 'Norwegian',
        'bg': 'Bulgarian', 'bul': 'Bulgarian',
        'sk': 'Slovak', 'slk': 'Slovak', 'slo': 'Slovak',
        'he': 'Hebrew', 'heb': 'Hebrew',
        'id': 'Indonesian', 'ind': 'Indonesian',
        'vi': 'Vietnamese', 'vie': 'Vietnamese',
        'th': 'Thai', 'tha': 'Thai',
        'ca': 'Catalan', 'cat': 'Catalan',
        'eu': 'Basque', 'eus': 'Basque', 'baq': 'Basque',
        'gl': 'Galician', 'glg': 'Galician',
        'lv': 'Latvian', 'lav': 'Latvian',
        'et': 'Estonian', 'est': 'Estonian',
        'hr': 'Croatian', 'hrv': 'Croatian',
        'sr': 'Serbian', 'srp': 'Serbian',
        'lt': 'Lithuanian', 'lit': 'Lithuanian',
        'sl': 'Slovenian', 'slv': 'Slovenian',
        'la': 'Latin', 'lat': 'Latin',
        'fa': 'Persian', 'fas': 'Persian', 'per': 'Persian',
        'bn': 'Bengali', 'ben': 'Bengali',
        'ur': 'Urdu', 'urd': 'Urdu'
    };

    function bcp47ToValidLanguage(code) {
        if (!code || typeof code !== 'string') return null;
        const clean = code.trim().toLowerCase();
        if (ISO_TO_VALID_LANG[clean]) return ISO_TO_VALID_LANG[clean];
        const primary = clean.split(/[-_]/)[0];
        if (ISO_TO_VALID_LANG[primary]) return ISO_TO_VALID_LANG[primary];
        for (const lang of availableLangs) {
            if (lang.code.toLowerCase() === clean) return lang.code;
        }
        return null;
    }

    function detectBookLanguage(doc) {
        try {
            const reader = (typeof window !== 'undefined') && (window.reader || window.book);
            if (reader) {
                const pkgMeta = reader.package && reader.package.metadata;
                const rawLang = (pkgMeta && (pkgMeta.language || pkgMeta.lang))
                    || (reader.metadata && (reader.metadata.language || reader.metadata.lang));
                if (rawLang) {
                    const langStr = Array.isArray(rawLang) ? rawLang[0] : (typeof rawLang === 'object' ? (rawLang.value || rawLang.code) : rawLang);
                    const matched = bcp47ToValidLanguage(String(langStr));
                    if (matched) return matched;
                }
            }
            if (doc) {
                const rootLang = doc.documentElement && (doc.documentElement.getAttribute('xml:lang') || doc.documentElement.getAttribute('lang') || doc.documentElement.lang);
                if (rootLang) {
                    const matched = bcp47ToValidLanguage(rootLang);
                    if (matched) return matched;
                }
                const bodyLang = doc.body && (doc.body.getAttribute('xml:lang') || doc.body.getAttribute('lang'));
                if (bodyLang) {
                    const matched = bcp47ToValidLanguage(bodyLang);
                    if (matched) return matched;
                }
                const metaLang = doc.querySelector && doc.querySelector('meta[name*="language" i], meta[http-equiv="content-language" i]');
                if (metaLang && metaLang.getAttribute('content')) {
                    const matched = bcp47ToValidLanguage(metaLang.getAttribute('content'));
                    if (matched) return matched;
                }
            }
        } catch (e) { /* ignore detection errors */ }
        return null;
    }

    // ── DOM Helpers ────────────────────────────────────────────────────
    function getReaderIframe() {
        if (READER_TYPE === 'kavita') return null;
        // A detached/closed document can still have queued MutationObserver
        // callbacks (notably during SPA teardown and test-window disposal).
        if (typeof document === 'undefined' || !document
                || typeof document.querySelector !== 'function') return null;
        return document.querySelector('#viewer iframe, .epub-container iframe, iframe');
    }

    function getReaderDoc() {
        if (READER_TYPE === 'kavita') {
            return document.querySelector('.book-content') ? document : null;
        }
        const iframe = getReaderIframe();
        if (iframe) {
            try { return iframe.contentDocument || iframe.contentWindow.document; } catch (e) { return null; }
        }
        return null;
    }

    function getReaderRoot() {
        if (READER_TYPE === 'kavita') {
            return document.querySelector('.book-content');
        }
        return getReaderDoc() || document;
    }

    const HEADING_CLASS_RE = /title|subtitle|chapter|heading|epigraph/i;

    function isHeading(el) {
        if (/^h[1-6]$/i.test(el.tagName)) return true;
        const c = (el.getAttribute && el.getAttribute('class')) || '';
        return HEADING_CLASS_RE.test(c);
    }

    function isCentered(el) {
        try {
            const win = el.ownerDocument.defaultView || window;
            return win.getComputedStyle(el).textAlign === 'center';
        } catch (e) { return false; }
    }

    function isPluginNode(el) {
        return !!(el.closest && el.closest('#bt-bar, #bt-menu, #bt-toast'))
            || (el.classList && (el.classList.contains('bt-translation') || el.classList.contains('bt-loading') || el.classList.contains('bt-feedback')));
    }

    // Canonical, de-duplicated set of translatable elements in a given document.
    function getTranslatableElements(doc) {
        if (!doc) return [];
        const rawElements = Array.from(doc.querySelectorAll(
            'p, blockquote, li, td, h1, h2, h3, h4, h5, h6, div.calibre1, div.text, a, ' +
            '[class*="title"], [class*="subtitle"], [class*="chapter"], [class*="author"], ' +
            '[class*="heading"], [class*="epigraph"], [class*="quote"], [class*="verse"]'
        ));

        // 1. Filter for content, layout, and exclusions.
        const filtered = rawElements.filter(el => {
            if (isPluginNode(el)) return false;                 // never translate our own UI
            const text = el.textContent.trim();
            if (text.length < 2) return false;

            const tagName = el.tagName.toLowerCase();

            if (tagName === 'a') {
                // Only standalone links (e.g. TOC entries); skip links inside prose.
                if (el.closest('p, div.calibre1, div.text, blockquote, li')) return false;
                return true;
            }

            // Blocks containing a link: let the link translate itself (keeps it clickable).
            if (['li', 'div', 'td'].includes(tagName) && el.querySelector('a')) return false;

            // Containers holding other block children: translate the children, not
            // the wrapper. `section`/`article` matter: chapter wrappers like
            // <section class="chapter"> match the [class*="chapter"] selector and,
            // unfiltered, get translated as ONE mega-block containing the whole
            // chapter (seen in production with a Calibre-converted epub).
            if (['div', 'blockquote', 'li', 'td', 'section', 'article', 'aside'].includes(tagName)
                && el.querySelector('p, h1, h2, h3, h4, h5, h6, li, blockquote, div.calibre1, div.text')) return false;

            return true;
        });

        // 2. De-duplicate hierarchy (O(n·depth)): when BOTH a wrapper and its inner
        // paragraphs are selected, keep the SMALLEST units (leaves) and drop the
        // ancestor. Keeping the ancestor — the previous behaviour — translated the
        // whole chapter as one giant block whenever a wrapper slipped through.
        const filteredSet = new Set(filtered);
        const ancestorsToDrop = new Set();
        for (const el of filtered) {
            let parent = el.parentElement;
            while (parent) {
                if (filteredSet.has(parent)) ancestorsToDrop.add(parent);
                parent = parent.parentElement;
            }
        }
        return filtered.filter(el => !ancestorsToDrop.has(el));
    }

    let _cachedParagraphs = null;
    let _cachedRoot = null;
    let _cachedGen = -1;

    function invalidateParagraphsCache() {
        _cachedParagraphs = null;
        _cachedRoot = null;
    }

    function getParagraphs() {
        const root = getReaderRoot();
        if (_cachedParagraphs && _cachedRoot === root && _cachedGen === generation) {
            if (_cachedParagraphs.length === 0 || (_cachedParagraphs[0] && _cachedParagraphs[0].isConnected)) {
                return _cachedParagraphs;
            }
        }
        const elements = getTranslatableElements(root);
        _cachedParagraphs = elements;
        _cachedRoot = root;
        _cachedGen = generation;
        return elements;
    }

    function getVisibleParagraphs() {
        // Filter the SAME canonical, de-duplicated set used everywhere else, so
        // visible-first covers headings/lists too and the prefetch complement is
        // exact (no element falls through the cracks between the two selectors).
        const iframe = getReaderIframe();
        const all = getParagraphs();
        if (READER_TYPE === 'kavita') {
            return all.filter(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const right = Number.isFinite(rect.right)
                    ? rect.right : rect.left + rect.width;
                const bottom = Number.isFinite(rect.bottom)
                    ? rect.bottom : rect.top + rect.height;
                return right >= -100 && rect.left < window.innerWidth - 20
                    && bottom >= -100 && rect.top < window.innerHeight - 20;
            });
        }
        if (!iframe || !iframe.contentDocument) {
            return all.slice(0, 5);
        }
        const iframeWidth = iframe.clientWidth || window.innerWidth;
        const iframeHeight = iframe.clientHeight || window.innerHeight;

        return all.filter(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return false;
            const isHorizVisible = (rect.left >= -100 && rect.left < iframeWidth - 20);
            const isVertVisible = (rect.top >= -100 && rect.top < iframeHeight - 20);
            return isHorizVisible && isVertVisible;
        });
    }

    function getParagraphText(el) {
        if (el.dataset.originalText) {
            paragraphTextCache.set(el, el.dataset.originalText);
            return el.dataset.originalText;
        }
        const cached = paragraphTextCache.get(el);
        if (cached !== undefined) return cached;
        const clone = el.cloneNode(true);
        clone.querySelectorAll('.bt-loading, .bt-translation, .bt-feedback').forEach(n => n.remove());
        const text = clone.textContent.trim();
        paragraphTextCache.set(el, text);
        return text;
    }

    function hashText(str) {
        // cyrb-style two-lane 64-bit string. Keep both unsigned 32-bit lanes
        // instead of coercing them into JavaScript's 53-bit Number range.
        let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
        for (let i = 0; i < str.length; i++) {
            const ch = str.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return (h2 >>> 0).toString(36).padStart(7, '0')
            + (h1 >>> 0).toString(36).padStart(7, '0');
    }

    function boundedScopeValue(value, fallback) {
        if (value === undefined || value === null) return fallback;
        const normalized = String(value).trim();
        return normalized ? normalized.slice(0, 512) : fallback;
    }

    function currentBookId() {
        if (READER_TYPE === 'kavita') {
            const route = kavitaRouteParts();
            return route ? `${route.libraryId}:${route.seriesId}` : 'unscoped';
        }
        if (cfg.bookId !== undefined && cfg.bookId !== null) {
            return boundedScopeValue(cfg.bookId, 'unscoped');
        }
        const match = window.location.pathname.match(/\/read\/([^/?#]+)/);
        if (!match) return 'unscoped';
        try {
            return boundedScopeValue(decodeURIComponent(match[1]), 'unscoped');
        } catch (e) {
            return boundedScopeValue(match[1], 'unscoped');
        }
    }

    function currentChapterId() {
        if (READER_TYPE === 'kavita') {
            const route = kavitaRouteParts();
            return route ? route.chapterId : 'unscoped';
        }
        try {
            const rendition = window.reader && window.reader.rendition;
            const location = rendition && rendition.currentLocation && rendition.currentLocation();
            const start = location && location.start;
            if (start) {
                // Prefer the stable chapter resource. Exact provider context is
                // fingerprinted server-side, while CFI changes on every page
                // turn and would fragment otherwise reusable cache entries.
                const identity = start.href || start.cfi || start.index;
                if (identity !== undefined && identity !== null) {
                    return boundedScopeValue(identity, 'unscoped');
                }
            }
        } catch (e) { /* reader is not ready yet */ }

        try {
            const iframe = getReaderIframe();
            if (iframe) {
                const doc = iframe.contentDocument;
                const identity = (doc && doc.documentURI) || iframe.getAttribute('src');
                if (identity) return boundedScopeValue(identity, 'unscoped');
            }
        } catch (e) { /* cross-origin iframe */ }
        return 'unscoped';
    }

    function translationScope() {
        return { book_id: currentBookId(), chapter_id: currentChapterId() };
    }

    // ── Glossary (per-book exact terms, server-injected into prompts) ──
    let glossaryEntries = [];
    let glossaryLoading = false;
    let glossaryLoadedBook = null;

    function loadGlossary() {
        if (!TRANSLATOR_URL) return Promise.resolve([]);
        const bookId = currentBookId();
        if (glossaryLoading || glossaryLoadedBook === bookId) {
            return Promise.resolve(glossaryEntries);
        }
        glossaryLoading = true;
        const scope = translationScope();
        const url = `${TRANSLATOR_URL}/glossary?book_id=${encodeURIComponent(scope.book_id)}` +
            `&chapter_id=${encodeURIComponent(scope.chapter_id)}`;
        return fetch(url, {
            headers: apiRequestHeaders(),
            credentials: apiRequestCredentials(),
        }).then((resp) => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return resp.json();
        }).then((data) => {
            if (data && Array.isArray(data.entries)) {
                glossaryEntries = data.entries;
                glossaryLoadedBook = bookId;
            }
            return glossaryEntries;
        }).catch(() => glossaryEntries).finally(() => {
            glossaryLoading = false;
        });
    }

    function saveGlossaryTerm(source, target) {
        if (!TRANSLATOR_URL) return Promise.resolve(false);
        const scope = translationScope();
        return fetch(`${TRANSLATOR_URL}/glossary`, {
            method: 'POST',
            headers: apiRequestHeaders({ json: true }),
            credentials: apiRequestCredentials(),
            body: JSON.stringify({
                source, target,
                book_id: scope.book_id, chapter_id: scope.chapter_id,
            }),
        }).then((resp) => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            glossaryLoadedBook = null;
            return loadGlossary().then(() => true);
        }).catch(() => false);
    }

    function deleteGlossaryTerm(source) {
        if (!TRANSLATOR_URL) return Promise.resolve(false);
        const scope = translationScope();
        return fetch(`${TRANSLATOR_URL}/glossary`, {
            method: 'DELETE',
            headers: apiRequestHeaders({ json: true }),
            credentials: apiRequestCredentials(),
            body: JSON.stringify({
                source, book_id: scope.book_id, chapter_id: scope.chapter_id,
            }),
        }).then((resp) => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            glossaryLoadedBook = null;
            return loadGlossary().then(() => true);
        }).catch(() => false);
    }

    // ── EPUB export (translated chapter keepsake, server-rebuilt) ──
    // Packages this session's translated paragraphs (insertion order
    // approximates reading order) into a minimal EPUB via POST
    // /export/epub, then downloads the rebuilt file. The request carries
    // the same auth transport and chapter scope as translation traffic.
    function exportTranslatedEpub() {
        const texts = Object.values(translatedParagraphs)
            .filter((tr) => !isBadTranslation(tr));
        if (!texts.length || !TRANSLATOR_URL) {
            showToast(!texts.length ? t.exportEmpty : t.exportFailed);
            return Promise.resolve(false);
        }
        const scope = translationScope();
        return fetch(`${TRANSLATOR_URL}/export/epub`, {
            method: 'POST',
            headers: apiRequestHeaders({ json: true }),
            credentials: apiRequestCredentials(),
            body: JSON.stringify({
                paragraphs: texts,
                title: (document.title || 'Translated chapter').slice(0, 200),
                source_lang: SOURCE_LANG,
                target_lang: TARGET_LANG,
                book_id: scope.book_id,
                chapter_id: scope.chapter_id,
            }),
        }).then((resp) => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return resp.blob();
        }).then((blob) => {
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = 'translation.epub';
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            setTimeout(() => URL.revokeObjectURL(url), 5000);
            showToast(t.exportDone);
            return true;
        }).catch(() => {
            showToast(t.exportFailed);
            return false;
        });
    }

    function elementContextId(el) {
        if (!el || !el.ownerDocument) return 'unscoped-element';
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node.parentElement) {
            const siblings = Array.from(node.parentElement.children).filter(sibling =>
                !sibling.classList.contains('bt-translation')
                && !sibling.classList.contains('bt-loading')
                && !sibling.classList.contains('bt-feedback'));
            const index = siblings.indexOf(node);
            parts.push(`${node.tagName.toLowerCase()}:${Math.max(0, index)}`);
            if (node.parentElement === node.ownerDocument.body) break;
            node = node.parentElement;
        }
        return parts.reverse().join('/') || 'unscoped-element';
    }

    function cacheKeyForText(text, elementContext = 'unscoped-element') {
        const scope = translationScope();
        return hashText(JSON.stringify([
            'reader-ui-cache/v4', READER_TYPE, BT_UI_VERSION, SOURCE_LANG, TARGET_LANG,
            scope.book_id, scope.chapter_id, elementContext, text
        ]));
    }

    // ── Translation engine ─────────────────────────────────────────────
    const FIRST_VISIBLE_CHUNK = 1; // minimize time to the first translated paragraph
    const VISIBLE_CHUNK = boundedInteger(cfg.batchSize, 1, 50, 5);
    const PREFETCH_CHUNK = VISIBLE_CHUNK;
    const PREFETCH_GAP_MS = boundedInteger(cfg.prefetchGapMs, 0, 10000, 0);
    const REQUEST_TIMEOUT_MS = 90000; // client-side safety net so a hung request can't freeze the UI

    // Server rejects paragraphs beyond BT_MAX_PARAGRAPH_CHARS (default 8000)
    // with a 413 that would fail the WHOLE batch. Skip oversized elements
    // client-side — they are almost always mis-detected wrappers, not prose.
    const CLIENT_MAX_PARAGRAPH_CHARS = 7500;

    function apiRequestCredentials() {
        return configuredCredentials || (
            AUTH_MODE === 'forwarded'
                ? 'include'
                : (AUTH_MODE === 'cwa_session' || AUTH_MODE === 'reader_session'
                    ? (cfg.sendCredentials === true ? 'include' : 'same-origin')
                    : 'omit')
        );
    }

    function apiRequestHeaders({ json = false } = {}) {
        const headers = {};
        if (json) headers['Content-Type'] = 'application/json';
        if (AUTH_MODE === 'token' && cfg.apiToken) {
            headers['X-BT-Token'] = cfg.apiToken;
        }
        return headers;
    }

    function validProviderPolicy(policy) {
        if (!policy || typeof policy !== 'object' || Array.isArray(policy)) return false;
        const keys = Object.keys(policy).sort();
        return keys.length === 3 && keys[0] === 'fallback'
            && keys[1] === 'generation' && keys[2] === 'primary'
            && (policy.primary === 'local' || policy.primary === 'remote')
            && (policy.fallback === null
                || policy.fallback === 'local'
                || policy.fallback === 'remote')
            && typeof policy.generation === 'string'
            && /^[a-f0-9]{32}$/.test(policy.generation);
    }

    async function loadProviderPolicy({ force = false } = {}) {
        if (!force && providerPolicyState) return true;
        if (providerPolicyPromise) return providerPolicyPromise;
        if (!TRANSLATOR_URL) return false;
        if (force) {
            providerPolicyState = null;
            allowCloudFallback = false;
            buildMenu();
        }
        providerPolicyPromise = (async () => {
            const send = () => fetch(`${TRANSLATOR_URL}/provider-policy`, {
                method: 'GET',
                headers: apiRequestHeaders(),
                credentials: apiRequestCredentials(),
                cache: 'no-store',
            });
            try {
                let response = await send();
                if (response.status === 401 && AUTH_MODE === 'reader_session'
                        && typeof window.__BT_REFRESH_SESSION === 'function') {
                    await window.__BT_REFRESH_SESSION();
                    response = await send();
                }
                if (!response.ok) return false;
                const policy = await response.json();
                if (!validProviderPolicy(policy)) return false;
                providerPolicyState = policy;
                if (policy.fallback !== 'remote') allowCloudFallback = false;
                buildMenu();
                return true;
            } catch (e) {
                console.error('[BookTranslator] provider privacy policy unavailable');
                return false;
            } finally {
                providerPolicyPromise = null;
            }
        })();
        return providerPolicyPromise;
    }

    function collectUncached(elements) {
        const out = [];
        const seen = new Set();
        for (const el of elements) {
            const text = getParagraphText(el);
            if (!text || text.length < 2) continue;
            if (text.length > CLIENT_MAX_PARAGRAPH_CHARS) {
                console.warn(`[BookTranslator] skipping oversized element (${text.length} chars) — likely a container, not a paragraph`);
                continue;
            }
            const hash = cacheKeyForText(text, elementContextId(el));
            if (translatedParagraphs[hash] || failedParagraphs.has(hash) || seen.has(hash)) continue;
            seen.add(hash);
            out.push({ el, text, hash });
        }
        return out;
    }

    async function postBatch(texts) {
        if (!TRANSLATOR_URL) {
            console.error('[BookTranslator] HTTPS requires a same-origin or TLS apiUrl');
            return { error: 'configuration' };
        }
        if (!await loadProviderPolicy()) {
            return { error: 'configuration' };
        }
        const controller = new AbortController();
        activeControllers.add(controller);
        // Distinguish OUR safety-net timeout from a deliberate abort
        // (newGeneration on mode/language/page change): a timeout is an
        // ambiguous terminal failure until the user explicitly retries; a
        // deliberate abort means the work is stale and can be discarded.
        const timer = setTimeout(() => { controller.btTimedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
        try {
            const headers = apiRequestHeaders({ json: true });
            const scope = translationScope();
            const requestCredentials = apiRequestCredentials();
            const requestBody = () => JSON.stringify({
                paragraphs: texts,
                source_lang: SOURCE_LANG,
                target_lang: TARGET_LANG,
                book_id: scope.book_id,
                chapter_id: scope.chapter_id,
                allow_cloud_fallback: allowCloudFallback,
                provider_policy: providerPolicyState
            });
            const send = () => fetch(`${TRANSLATOR_URL}/translate/batch`, {
                method: 'POST',
                headers,
                credentials: requestCredentials,
                body: requestBody(),
                signal: controller.signal,
            });
            let resp = await send();
            // A 401 is rejected before translation admission, so one session
            // refresh and one replay cannot duplicate provider work. No other
            // ambiguous failure is retried automatically.
            if (resp.status === 401 && AUTH_MODE === 'reader_session'
                    && typeof window.__BT_REFRESH_SESSION === 'function') {
                try {
                    await window.__BT_REFRESH_SESSION();
                } catch (e) {
                    return null;
                }
                if (!await loadProviderPolicy({ force: true })) {
                    return { error: 'configuration' };
                }
                if (controller.signal.aborted) {
                    return { error: controller.btTimedOut ? 'timeout' : 'aborted' };
                }
                resp = await send();
            }
            if (!resp.ok) {
                if (resp.status === 409) {
                    providerPolicyState = null;
                    allowCloudFallback = false;
                    await loadProviderPolicy({ force: true });
                    return { error: 'policy_changed' };
                }
                if (resp.status === 429) {
                    let r = {};
                    try { r = await resp.json(); } catch(e) {}
                    const safeAdmission = r.retry_safe === true
                        && (r.scope === 'api_admission'
                            || r.scope === 'auth_admission');
                    if (!safeAdmission) {
                        return { error: 'provider_unavailable' };
                    }
                    let after = Number(r.retry_after || resp.headers.get('Retry-After'));
                    if (!Number.isFinite(after) || after <= 0) {
                        after = BT_CLIENT_RATE_LIMIT_BACKOFF_MS / 1000;
                    }
                    after = Math.min(BT_CLIENT_MAX_RETRY_AFTER_SECONDS, Math.max(1, after));
                    return { error: 'rate_limited', retry_after: after };
                }
                return null;
            }
            return await resp.json();
        } catch (e) {
            if (e.name === 'AbortError') {
                return { error: controller.btTimedOut ? 'timeout' : 'aborted' };
            }
            throw e;
        } finally {
            clearTimeout(timer);
            activeControllers.delete(controller);
        }
    }


    function renderStreamingProgress(el, partialText) {
        if (!el || !partialText) return;
        if (translationMode === 'bilingual') {
            let transEl = el.querySelector(':scope > .bt-translation');
            if (!transEl) {
                const heading = isHeading(el);
                transEl = el.ownerDocument.createElement(heading ? 'div' : 'span');
                transEl.className = 'bt-translation bt-streaming-live ' + (heading ? 'bt-heading-translation' : 'bt-translation-bilingual');
                transEl.setAttribute('dir', 'auto');
                if (heading && isCentered(el)) transEl.className += ' bt-center';
                el.appendChild(transEl);
            }
            transEl.textContent = partialText;
        } else if (translationMode === 'translated') {
            if (!el.dataset.btOriginal) {
                el.dataset.btOriginal = el.innerHTML;
            }
            el.textContent = partialText;
        }
    }

    async function postStream(text, onChunk) {
        if (!TRANSLATOR_URL || !window.ReadableStream) {
            return postSingle(text);
        }
        if (!await loadProviderPolicy()) {
            return { error: 'configuration' };
        }
        const controller = new AbortController();
        activeControllers.add(controller);
        const timer = setTimeout(() => { controller.btTimedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
        try {
            const headers = apiRequestHeaders({ json: true });
            const scope = translationScope();
            const requestCredentials = apiRequestCredentials();
            const requestBody = () => JSON.stringify({
                text: text,
                source_lang: SOURCE_LANG,
                target_lang: TARGET_LANG,
                book_id: scope.book_id,
                chapter_id: scope.chapter_id,
                allow_cloud_fallback: allowCloudFallback,
                provider_policy: providerPolicyState
            });
            const resp = await fetch(`${TRANSLATOR_URL}/translate/stream`, {
                method: 'POST',
                headers,
                credentials: requestCredentials,
                body: requestBody(),
                signal: controller.signal,
            });
            if (!resp.ok) {
                return postSingle(text);
            }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let completeText = '';
            let backend = 'local';
            let cached = false;
            let elapsed_ms = 0;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (let i = 0; i < lines.length; i++) {
                    const line = lines[i].trim();
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.slice(6));
                            if (data.delta) {
                                completeText += data.delta;
                                if (typeof onChunk === 'function') onChunk(completeText);
                            }
                            if (data.translated) completeText = data.translated;
                            if (data.backend) backend = data.backend;
                            if (data.cached !== undefined) cached = data.cached;
                            if (data.elapsed_ms) elapsed_ms = data.elapsed_ms;
                        } catch (err) {}
                    }
                }
            }
            return {
                translated: completeText,
                backend: backend,
                cached: cached,
                elapsed_ms: elapsed_ms
            };
        } catch (e) {
            if (e.name === 'AbortError') {
                return { error: controller.btTimedOut ? 'timeout' : 'aborted' };
            }
            return postSingle(text);
        } finally {
            clearTimeout(timer);
            activeControllers.delete(controller);
        }
    }

    async function postSingle(text) {
        if (!TRANSLATOR_URL) {
            console.error('[BookTranslator] HTTPS requires a same-origin or TLS apiUrl');
            return { error: 'configuration' };
        }
        if (!await loadProviderPolicy()) {
            return { error: 'configuration' };
        }
        const controller = new AbortController();
        activeControllers.add(controller);
        const timer = setTimeout(() => { controller.btTimedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
        try {
            const headers = apiRequestHeaders({ json: true });
            const scope = translationScope();
            const requestCredentials = apiRequestCredentials();
            const requestBody = () => JSON.stringify({
                text: text,
                source_lang: SOURCE_LANG,
                target_lang: TARGET_LANG,
                book_id: scope.book_id,
                chapter_id: scope.chapter_id,
                allow_cloud_fallback: allowCloudFallback,
                provider_policy: providerPolicyState
            });
            const send = () => fetch(`${TRANSLATOR_URL}/translate`, {
                method: 'POST',
                headers,
                credentials: requestCredentials,
                body: requestBody(),
                signal: controller.signal,
            });
            let resp = await send();
            if (resp.status === 401 && AUTH_MODE === 'reader_session'
                    && typeof window.__BT_REFRESH_SESSION === 'function') {
                try {
                    await window.__BT_REFRESH_SESSION();
                } catch (e) {
                    return null;
                }
                if (!await loadProviderPolicy({ force: true })) {
                    return { error: 'configuration' };
                }
                if (controller.signal.aborted) {
                    return { error: controller.btTimedOut ? 'timeout' : 'aborted' };
                }
                resp = await send();
            }
            if (!resp.ok) {
                if (resp.status === 409) {
                    providerPolicyState = null;
                    allowCloudFallback = false;
                    await loadProviderPolicy({ force: true });
                    return { error: 'policy_changed' };
                }
                if (resp.status === 429) {
                    let r = {};
                    try { r = await resp.json(); } catch(e) {}
                    const safeAdmission = r.retry_safe === true
                        && (r.scope === 'api_admission'
                            || r.scope === 'auth_admission');
                    if (!safeAdmission) {
                        return { error: 'provider_unavailable' };
                    }
                    let after = Number(r.retry_after || resp.headers.get('Retry-After'));
                    if (!Number.isFinite(after) || after <= 0) {
                        after = BT_CLIENT_RATE_LIMIT_BACKOFF_MS / 1000;
                    }
                    after = Math.min(BT_CLIENT_MAX_RETRY_AFTER_SECONDS, Math.max(1, after));
                    return { error: 'rate_limited', retry_after: after };
                }
                return null;
            }
            return await resp.json();
        } catch (e) {
            if (e.name === 'AbortError') {
                return { error: controller.btTimedOut ? 'timeout' : 'aborted' };
            }
            throw e;
        } finally {
            clearTimeout(timer);
            activeControllers.delete(controller);
        }
    }

    async function pumpQueue() {
        if (isPumpRunning) return;
        isPumpRunning = true;
        
        try {
            while (translationMode !== 'off' && readerRouteActive && !isOffline) {
                const now = Date.now();
                if (rateLimitUntil > now) {
                    refreshStatus();
                    await new Promise(r => setTimeout(r, Math.min(1000, rateLimitUntil - now)));
                    continue;
                }
                
                // Cleanup stale items and deduplicate
                const seenHash = new Set();
                visibleQueue = visibleQueue.filter(x => {
                    if (x.gen !== generation || translatedParagraphs[x.hash] || seenHash.has(x.hash)) return false;
                    seenHash.add(x.hash);
                    return true;
                });
                prefetchQueue = prefetchQueue.filter(x => {
                    if (!prefetchEnabled) return false;
                    if (x.gen !== generation || translatedParagraphs[x.hash] || seenHash.has(x.hash)) return false;
                    seenHash.add(x.hash);
                    return true;
                });
                
                if (visibleQueue.length === 0 && prefetchQueue.length === 0) {
                    break; // Nothing to do
                }

                if (visibleQueue.length === 0 && prefetchQueue.length > 0
                        && nextPrefetchAt > now) {
                    isPrefetching = false;
                    refreshStatus();
                    await waitForPrefetchGap(nextPrefetchAt - now);
                    continue;
                }
                
                let isVisible = false;
                let batch = [];
                if (visibleQueue.length > 0) {
                    const chunkSize = firstVisibleBatchCompleted
                        ? VISIBLE_CHUNK : FIRST_VISIBLE_CHUNK;
                    batch = visibleQueue.slice(0, chunkSize);
                    visibleQueue = visibleQueue.slice(chunkSize);
                    isVisible = true;
                } else {
                    batch = prefetchQueue.slice(0, PREFETCH_CHUNK);
                    prefetchQueue = prefetchQueue.slice(PREFETCH_CHUNK);
                }
                
                isTranslating = isVisible;
                isPrefetching = !isVisible;
                inflightCount = batch.length;
                refreshStatus();

                // Network failures and timeouts are ambiguous: the server may
                // still be translating after the browser gives up. Mark them
                // terminal for this session and require an explicit user retry.
                const markBatchFailed = (items) => {
                    items.forEach(x => {
                        failedParagraphs.add(x.hash);
                        rateLimitResponses.delete(x.hash);
                    });
                    chapterDone += items.length;
                    errorCount++;
                };

                // A 429 is safe to retry because the server rejected admission
                // before provider work. Bound both attempts and Retry-After so
                // a broken proxy cannot hold the queue forever.
                const requeueRateLimited = (items) => {
                    const keep = [];
                    const dropped = [];
                    items.forEach(x => {
                        const responses = (rateLimitResponses.get(x.hash) || 0) + 1;
                        rateLimitResponses.set(x.hash, responses);
                        if (responses < BT_CLIENT_MAX_RATE_LIMIT_RESPONSES) keep.push(x);
                        else dropped.push(x);
                    });
                    if (dropped.length) markBatchFailed(dropped);
                    if (isVisible) visibleQueue.unshift(...keep);
                    else prefetchQueue.unshift(...keep);
                    return keep.length;
                };

                let data = null;
                nextPrefetchAt = Date.now() + PREFETCH_GAP_MS;
                try {
                    data = await postBatch(batch.map(b => b.text));
                } catch (e) {
                    console.error("Translation request failed:", e);
                    markBatchFailed(batch);
                    inflightCount = 0;
                    refreshStatus();
                    continue;
                }

                inflightCount = 0;

                // Stale-response guard: page/language/mode may have changed
                // while the request was in flight. Never let an old batch
                // pollute cache, counters, or DOM.
                if ((batch.length ? batch[0].gen : generation) !== generation
                        || translationMode === 'off' || !readerRouteActive) {
                    refreshStatus();
                    continue;
                }

                if (data && data.error === 'aborted') {
                    // Deliberate cancel (mode/language/page change) — the items
                    // belong to a stale generation and get filtered next pass.
                    continue;
                }

                if (data && data.error === 'rate_limited') {
                    if (requeueRateLimited(batch) > 0) {
                        rateLimitUntil = Date.now() + (data.retry_after * 1000);
                    }
                    // errorCount not incremented for rate limit
                    refreshStatus();
                    continue;
                }

                if (!data || data.error === 'timeout' || !Array.isArray(data.translations)) {
                    markBatchFailed(batch);
                    refreshStatus();
                    continue;
                }

                let stored = false, anyGood = false;
                data.translations.forEach((tr, idx) => {
                    if (idx >= batch.length) return; // defensive: never trust response length
                    if (!isBadTranslation(tr)) {
                        translatedParagraphs[batch[idx].hash] = tr;
                        rateLimitResponses.delete(batch[idx].hash);
                        stored = true;
                        anyGood = true;
                    }
                });

                // Split the batch into translated vs failed (backend error
                // markers / empty). Only an explicit user action retries a
                // failed paragraph; successful entries remain available.
                const succeeded = batch.filter(b => translatedParagraphs[b.hash]);
                chapterDone += succeeded.length;
                if (isVisible && succeeded.length > 0) {
                    firstVisibleBatchCompleted = true;
                }
                const failed = batch.filter(b => !translatedParagraphs[b.hash]);
                if (failed.length) markBatchFailed(failed);
                else if (anyGood) errorCount = 0;
                if (!isVisible) isPrefetching = false;
                else isTranslating = false;
                refreshStatus();
                
                if (stored) {
                    schedulePersist();
                    if (isVisible && batch[0].gen === generation) {
                        renderMode(batch.map(b => b.el));
                    }
                }
            }
        } finally {
            isPumpRunning = false;
            isTranslating = false;
            isPrefetching = false;
            refreshStatus();
        }
    }

    async function translateCurrentPage() {
        if (translationMode === 'off' || !readerRouteActive) return;
        
        const myGen = generation;
        const idoc = getReaderDoc();
        if (idoc) {
            const detected = detectBookLanguage(idoc);
            if (detected && detected !== detectedSourceLang) {
                detectedSourceLang = detected;
                if (SOURCE_LANG_SETTING === 'Auto') {
                    SOURCE_LANG = detected;
                    const sourcePicker = document.getElementById('bt-source-lang');
                    if (sourcePicker) sourcePicker.innerHTML = sourceLanguageOptions(SOURCE_LANG_SETTING, detectedSourceLang);
                }
            }
        }
        if (idoc && READER_TYPE === 'cwa') {
            ensureIframeStyles(idoc);
            applyIframeTheme(idoc);
        }

        const visibleEls = getVisibleParagraphs();
        
        // Paint any visible paragraphs that were already cached (revisited page).
        renderMode(visibleEls);

        visibleQueue = collectUncached(visibleEls).map(x => ({...x, gen: myGen}));
        
        const allParagraphs = getParagraphs();
        const visibleSet = new Set(visibleEls);

        // Directional Lookahead: find the boundary of visible elements in the full chapter
        let lastVisibleIdx = -1;
        for (let i = allParagraphs.length - 1; i >= 0; i--) {
            if (visibleSet.has(allParagraphs[i])) {
                lastVisibleIdx = i;
                break;
            }
        }

        // Priority 1: Forward paragraphs (the next pages ahead in reading order)
        const forwardEls = (lastVisibleIdx >= 0)
            ? allParagraphs.slice(lastVisibleIdx + 1).filter(el => !visibleSet.has(el))
            : allParagraphs.filter(el => !visibleSet.has(el));

        // Combined prefetch queue: bounded forward lookahead only (max 8 paragraphs ahead)
        const MAX_PREFETCH_AHEAD = boundedInteger(cfg.maxPrefetchParagraphs, 1, 50, 8);
        const forwardSlice = forwardEls.slice(0, MAX_PREFETCH_AHEAD);
        const prefetchEls = prefetchEnabled ? forwardSlice : [];
        prefetchQueue = collectUncached(prefetchEls).map(x => ({...x, gen: myGen}));
        // No snapshot total here: refreshStatus derives done/total live from
        // chapterDone + inflight + queues (see chapterProgress), so re-triggers
        // on page turns / iframe mutations can only ADD newly-discovered work.

        refreshStatus();
        // A running pump may currently own an interruptible background delay.
        // Wake it after publishing the new queues so visible work is admitted
        // immediately instead of inheriting prefetch pacing.
        if (prefetchWaitWake) prefetchWaitWake();
        pumpQueue();
    }

    function triggerPrefetch() {
        if (!prefetchEnabled || translationMode === 'off') return;
        pumpQueue();
    }

    // ── Iframe styling (parent-page CSS does not cascade into the EPUB iframe) ──
    const IFRAME_STYLE_ID = 'bt-injected-styles';
    const IFRAME_CSS = `
:root,html{--bt-translation-color:#1565c0;--bt-translation-border:#90caf9;--bt-translation-bg:rgba(21,101,192,0.06);}
html[data-bt-theme="dark"]{--bt-translation-color:#8ec0f9;--bt-translation-border:#1976d2;--bt-translation-bg:rgba(142,192,249,0.10);}
html[data-bt-theme="sepia"]{--bt-translation-color:#6d4c41;--bt-translation-border:#a1887f;--bt-translation-bg:rgba(109,76,65,0.08);}
.bt-translation{display:block;margin:0.5em 0 0.25em;padding:0.15em 0 0.15em 0.7em;border-left:3px solid var(--bt-translation-border);background:var(--bt-translation-bg);color:var(--bt-translation-color)!important;font-style:italic;font-weight:normal;line-height:1.5;}
.bt-heading-translation{border-left:none;background:transparent;padding-left:0;font-size:0.72em;opacity:0.92;margin-top:0.3em;break-inside:avoid;page-break-inside:avoid;}
.bt-center{text-align:center;}
.bt-loading{opacity:0.6;font-style:italic;}
`;

    function ensureIframeStyles(idoc) {
        try {
            if (!idoc || idoc.getElementById(IFRAME_STYLE_ID)) return;
            const style = idoc.createElement('style');
            style.id = IFRAME_STYLE_ID;
            style.textContent = IFRAME_CSS;
            (idoc.head || idoc.documentElement).appendChild(style);
        } catch (e) { /* cross-origin or detached doc — ignore */ }
    }

    function applyIframeTheme(idoc) {
        try {
            if (!idoc || !idoc.body) return;
            const win = idoc.defaultView || window;
            const m = (win.getComputedStyle(idoc.body).backgroundColor || '').match(/\d+/g);
            let theme = 'light';
            if (m && m.length >= 3) {
                const [r, g, b] = m.map(Number);
                const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                if (lum < 110) theme = 'dark';
                else if (r >= g && g > b && (r - b) > 12) theme = 'sepia';
            }
            idoc.documentElement.dataset.btTheme = theme;
        } catch (e) { /* ignore */ }
    }

    // ── Rendering ──────────────────────────────────────────────────────
    // Cloned original children of inline-replaced elements. dataset.originalText
    // (plain text) remains the marker + hash source, but restoring from it
    // would permanently strip markup. Keeping nodes avoids reparsing EPUB
    // markup through an innerHTML sink during restoration.
    const originalContent = new WeakMap();

    function restoreOriginal(el) {
        if (el.dataset.originalText === undefined) return;
        const fragment = originalContent.get(el);
        if (fragment !== undefined) el.replaceChildren(fragment);
        else el.textContent = el.dataset.originalText; // fallback (pre-fix entries)
        originalContent.delete(el);
        delete el.dataset.originalText;
    }

    function showTranslationsBilingual(paragraphs) {
        paragraphs.forEach((el) => {
            const text = getParagraphText(el);
            if (!text) return;
            const hash = cacheKeyForText(text, elementContextId(el));
            const translated = translatedParagraphs[hash];
            if (isBadTranslation(translated) || translated === text) return;

            // If this element was previously inline-translated, restore the clean
            // original (with its markup) first so we never stack a bilingual
            // block onto replaced text.
            restoreOriginal(el);

            // Idempotent: update the existing direct-child translation instead of duplicating.
            let transEl = el.querySelector(':scope > .bt-translation');
            if (transEl) {
                transEl.textContent = translated;
            } else {
                const heading = isHeading(el);
                transEl = el.ownerDocument.createElement(heading ? 'div' : 'span');
                transEl.className = 'bt-translation ' + (heading ? 'bt-heading-translation' : 'bt-translation-bilingual');
                transEl.setAttribute('dir', 'auto');
                if (heading && isCentered(el)) transEl.className += ' bt-center';
                transEl.textContent = translated;
                el.appendChild(transEl);
            }
            attachFeedbackControls(transEl, hash);
        });
    }

    // ── Feedback (per-paragraph thumbs, bilingual mode only) ────────────
    // Buttons render as a sibling AFTER .bt-translation so the translation
    // node's textContent stays exactly the translated string. Every overlay
    // guard that knows .bt-translation must also know .bt-feedback
    // (isBtNode/isPluginNode, getParagraphText, elementContextId,
    // removeAllTranslations, restoreOriginal). Handlers are assigned
    // directly because translations may render inside the reader iframe,
    // outside the parent document's delegated listeners. The paragraph key
    // is the opaque client cache hash — raw book text is never sent.
    function sendFeedback(paraKey, rating, controls) {
        if (!TRANSLATOR_URL || !paraKey) return Promise.resolve(false);
        const scope = translationScope();
        return fetch(`${TRANSLATOR_URL}/feedback`, {
            method: 'POST',
            headers: apiRequestHeaders({ json: true }),
            credentials: apiRequestCredentials(),
            body: JSON.stringify({
                para_key: paraKey, rating,
                book_id: scope.book_id, chapter_id: scope.chapter_id,
            }),
        }).then((resp) => {
            if (!resp.ok) return false;
            if (controls) {
                controls.querySelectorAll('.bt-fb-btn').forEach((btn) => {
                    const active = btn.dataset.rating === String(rating);
                    btn.classList.toggle('bt-fb-active', active);
                    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
                });
            }
            return true;
        }).catch(() => false);
    }

    function attachFeedbackControls(transEl, paraKey) {
        if (!transEl || !paraKey || !transEl.ownerDocument) return;
        // Sibling (not child): .bt-translation textContent stays exactly the
        // translated string, which the frontend contract tests assert.
        const host = transEl.parentElement;
        if (!host) return;
        let fb = host.querySelector(':scope > .bt-feedback');
        if (!fb) {
            fb = transEl.ownerDocument.createElement('span');
            fb.className = 'bt-feedback';
            const mk = (rating, glyph, label) => {
                const btn = transEl.ownerDocument.createElement('button');
                btn.type = 'button';
                btn.className = 'bt-fb-btn';
                btn.dataset.rating = String(rating);
                btn.textContent = glyph;
                btn.title = label;
                btn.setAttribute('aria-label', label);
                btn.setAttribute('aria-pressed', 'false');
                btn.onclick = (e) => {
                    e.stopPropagation();
                    sendFeedback(fb.dataset.paraKey, rating, fb);
                };
                return btn;
            };
            fb.appendChild(mk(1, '👍', t.fbUp));
            fb.appendChild(mk(-1, '👎', t.fbDown));
            transEl.after(fb);
        }
        fb.dataset.paraKey = paraKey;
    }

    function showTranslationsInline(mode, paragraphs) {
        paragraphs.forEach((el) => {
            const text = getParagraphText(el);
            if (!text) return;
            const hash = cacheKeyForText(text, elementContextId(el));
            const translated = translatedParagraphs[hash];
            if (isBadTranslation(translated)) return;

            // Store the CLEAN original so toggling back restores correctly even
            // after bilingual rendering: plain text in dataset (marker + hash
            // source) and cloned markup nodes in the WeakMap (see restoreOriginal).
            if (!el.dataset.originalText) {
                el.dataset.originalText = text;
                const clone = el.cloneNode(true);
                clone.querySelectorAll('.bt-translation, .bt-loading, .bt-feedback').forEach(n => n.remove());
                const fragment = el.ownerDocument.createDocumentFragment();
                while (clone.firstChild) fragment.appendChild(clone.firstChild);
                originalContent.set(el, fragment);
            }
            // Remove any bilingual/loading/feedback spans before replacing the text.
            el.querySelectorAll('.bt-translation, .bt-loading, .bt-feedback').forEach(n => n.remove());
            el.textContent = translated;
        });
    }

    function removeAllTranslations() {
        document.querySelectorAll('.bt-translation, .bt-loading, .bt-feedback').forEach(el => el.remove());

        const iframe = getReaderIframe();
        if (iframe && iframe.contentDocument) {
            iframe.contentDocument.querySelectorAll('.bt-translation, .bt-loading, .bt-feedback').forEach(el => el.remove());
        }

        const restoreIn = (root) => {
            root.querySelectorAll('[data-original-text]').forEach(restoreOriginal);
        };
        restoreIn(document);
        if (iframe && iframe.contentDocument) restoreIn(iframe.contentDocument);
    }

    // ── Observers & Polling ────────────────────────────────────────────
    const isBtNode = (node) => {
        if (!node || node.nodeType !== 1) return false;
        return !!(node.closest && node.closest(
            '#bt-bar, #bt-menu, #bt-toast, .bt-translation, .bt-loading, .bt-feedback'
        ));
    };

    function mutationContainsReaderContent(mutation) {
        const target = mutation.target && mutation.target.nodeType === 1
            ? mutation.target
            : mutation.target && mutation.target.parentElement;
        if (target && target.closest
                && target.closest('#bt-bar, #bt-menu, #bt-toast, [data-original-text]')) {
            return false;
        }
        if (mutation.addedNodes.length === 0) return true;
        return Array.from(mutation.addedNodes).some(node => !isBtNode(node));
    }

    let translateTimeout = null;
    let lastContentIdentity = null;
    let readerObserver = null;
    let mainObserver = null;
    const watchedReaderIframes = new WeakSet();

    function setOverlayHidden(hidden) {
        ['bt-bar', 'bt-menu', 'bt-toast'].forEach(id => {
            const element = document.getElementById(id);
            if (element) {
                element.hidden = hidden;
                // Author CSS can override the user-agent [hidden] rule (the
                // toolbar normally uses display:flex), so enforce route
                // deactivation at the inline cascade as well.
                element.style.display = hidden ? 'none' : '';
            }
        });
        if (hidden) closeMenu();
    }

    function syncReaderRoute({ initial = false } = {}) {
        const supported = isSupportedReaderRoute();
        const wasActive = readerRouteActive;
        if (!supported) {
            if (wasActive) {
                readerRouteActive = false;
                clearTimeout(translateTimeout);
                newGeneration();
                removeAllTranslations();
            }
            setOverlayHidden(true);
            return false;
        }

        readerRouteActive = true;
        createFloatingUI();
        applyBarPosition();
        setOverlayHidden(false);
        if (!wasActive && !initial && translationMode !== 'off') {
            lastContentIdentity = null;
            attachReaderContentObserver();
            scheduleTranslate('reader_route', { immediate: true, forceRediscover: true });
        }
        return true;
    }

    function scheduleTranslate(reason, { immediate = false, forceRediscover = false } = {}) {
        if (translationMode === 'off' || !readerRouteActive) return;
        lastTriggerReason = reason;

        if (forceRediscover) {
            newGeneration(); // Cancel stale work immediately if it's a chapter/page turn
            lastFirstVisibleHash = null; // force the detector to pick up the new page
        }

        clearTimeout(translateTimeout);
        if (immediate) {
            translateCurrentPage();
        } else {
            translateTimeout = setTimeout(() => {
                translateCurrentPage();
            }, 250);
        }
    }

    function watchReaderIframe(iframe) {
        if (!iframe || watchedReaderIframes.has(iframe)) return;
        watchedReaderIframes.add(iframe);
        iframe.addEventListener('load', () => {
            if (readerRouteActive) {
                attachReaderContentObserver({ rediscover: true });
            }
        });
    }

    function attachReaderContentObserver({ rediscover = false } = {}) {
        let content = null;
        if (READER_TYPE === 'kavita') {
            content = getReaderRoot();
        } else {
            const iframe = getReaderIframe();
            watchReaderIframe(iframe);
            try {
                const idoc = iframe && (
                    iframe.contentDocument || iframe.contentWindow.document
                );
                content = idoc && idoc.body;
            } catch (e) { content = null; }
        }
        if (!content || content === lastContentIdentity) return false;

        const replacesObservedContent = lastContentIdentity !== null;
        lastContentIdentity = content;
        invalidateParagraphsCache();
        if (readerObserver) readerObserver.disconnect();
        readerObserver = new MutationObserver((mutations) => {
            if (!readerRouteActive
                    || !mutations.some(mutationContainsReaderContent)) return;
            scheduleTranslate(
                READER_TYPE === 'kavita'
                    ? 'kavita_content_mutation' : 'iframe_mutation',
                { forceRediscover: READER_TYPE === 'kavita' }
            );
        });
        if (READER_TYPE === 'cwa') {
            const idoc = content.ownerDocument;
            ensureIframeStyles(idoc);
            applyIframeTheme(idoc);
            attachIframeShortcut(idoc);
        }
        readerObserver.observe(content, { childList: true, subtree: true });
        if (rediscover && translationMode !== 'off') {
            scheduleTranslate('new_reader_content', {
                immediate: true,
                // First attachment may race with work discovered by the main
                // observer. Only a confirmed document replacement makes that
                // work stale enough to abort and replay.
                forceRediscover: replacesObservedContent
            });
        }
        return true;
    }

    function setupObservers() {
        if (!mainObserver) {
            mainObserver = new MutationObserver((mutations) => {
                if (!readerRouteActive) return;
                const relevant = mutations.some(mutationContainsReaderContent);
                if (relevant) {
                    // Reconcile the reader root before it can admit work. The
                    // content observer handles mutations inside the current
                    // root; this path handles a root/iframe replacement.
                    attachReaderContentObserver({ rediscover: true });
                }
            });
            mainObserver.observe(document.body, { childList: true, subtree: true });
        }

        // The reader document normally exists by DOMContentLoaded. Attach now
        // so the first polling tick cannot mistake it for a chapter change and
        // abort already-admitted provider work. The poll remains as a fallback
        // for readers that replace or create their content asynchronously.
        attachReaderContentObserver();

        // Track CWA iframe documents and Kavita's stable .book-content host.
        setInterval(() => {
            if (!syncReaderRoute()) return;
            attachReaderContentObserver({ rediscover: true });
            if (translationMode === 'off') return;

            // Position-based page turn detector.
            // BUG (root cause of the status bar flicker): inserting a bilingual
            // translation block under a paragraph increases that paragraph's
            // rendered height, which reflows the layout and can shift WHICH
            // paragraph counts as "first visible" — with no real page turn.
            // That false positive used to call scheduleTranslate(forceRediscover:true)
            // unconditionally, which hides the status pill (newGeneration resets
            // isTranslating/isPrefetching -> refreshStatus) and immediately shows
            // it again (translateCurrentPage sets isTranslating=true ->
            // refreshStatus) in the same tick. Because our own rendering keeps
            // shifting the layout throughout an active translation pass, this
            // repeated every ~350ms poll for as long as work was in progress —
            // the pill blinking on/off is that hide+show cycle repeating.
            //
            // Fix: only poll for page turns while genuinely idle (no
            // translation/prefetch in flight), so our own layout shifts can't
            // feed back into this detector. Real navigation while work is in
            // flight is still caught immediately via the epub.js relocated/
            // rendered hooks below (attachEpubHooks), which don't depend on
            // visual position at all. Also require the new position to be seen
            // on two consecutive polls (~700ms apart) before accepting it, as a
            // second line of defense against any other transient layout blip.
            // Check for page turns even while prefetching in background!
            // Background prefetch does not shift visible layout.
            if (!isTranslating && !isPrefetching) {
                const visible = getVisibleParagraphs();
                if (visible.length > 0) {
                    const firstText = getParagraphText(visible[0]);
                    if (firstText) {
                        const hash = hashText(firstText);
                        if (hash !== lastFirstVisibleHash) {
                            if (hash === pendingFirstVisibleHash) {
                                // Seen on the previous poll too — confirmed, not a blip.
                                lastFirstVisibleHash = hash;
                                pendingFirstVisibleHash = null;
                                scheduleTranslate('page_turn', { immediate: true, forceRediscover: true });
                            } else {
                                pendingFirstVisibleHash = hash;
                            }
                        } else {
                            pendingFirstVisibleHash = null;
                        }
                    }
                }
            }
        }, 350);
    }

    function attachEpubHooks() {
        if (READER_TYPE !== 'cwa') return;
        if (window.reader && window.reader.rendition) {
            window.reader.rendition.on('relocated', () => {
                scheduleTranslate('epub_relocated', { immediate: true, forceRediscover: true });
            });
            window.reader.rendition.on('rendered', () => {
                scheduleTranslate('epub_rendered', { immediate: true, forceRediscover: true });
            });
        } else {
            setTimeout(attachEpubHooks, 1000);
        }
    }

    // ── Start ──────────────────────────────────────────────────────────
    function onShortcutKeydown(e) {
        // Alt+T cycles the mode (Ctrl/Cmd+T is reserved by the browser for new tabs).
        if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 't' || e.key === 'T')) {
            e.preventDefault();
            const next = translationMode === 'off' ? 'bilingual'
                : translationMode === 'bilingual' ? 'translated' : 'off';
            setMode(next);
        }
    }

    function onNavKeydown(e) {
        if (translationMode === 'off' || !readerRouteActive) return;
        const navKeys = ['ArrowRight', 'ArrowLeft', 'PageDown', 'PageUp', ' '];
        if (navKeys.includes(e.key) && !e.altKey && !e.ctrlKey && !e.metaKey) {
            setTimeout(() => {
                if (readerRouteActive && translationMode !== 'off') {
                    const visible = getVisibleParagraphs();
                    if (visible.length > 0) {
                        const firstText = getParagraphText(visible[0]);
                        if (firstText) {
                            const hash = hashText(firstText);
                            if (hash !== lastFirstVisibleHash) {
                                lastFirstVisibleHash = hash;
                                scheduleTranslate('nav_key', { immediate: true, forceRediscover: true });
                            }
                        }
                    }
                }
            }, 80);
        }
    }

    function setupKeyboardShortcut() {
        document.addEventListener('keydown', onShortcutKeydown);
        document.addEventListener('keydown', onNavKeydown);
    }

    // The reader iframe swallows key events when it has focus (which it almost
    // always does while reading) — attach the same shortcut inside each new
    // chapter document so Alt+T works regardless of focus.
    function attachIframeShortcut(idoc) {
        try {
            if (!idoc || idoc.btShortcutAttached) return;
            idoc.btShortcutAttached = true;
            idoc.addEventListener('keydown', onShortcutKeydown);
            idoc.addEventListener('keydown', onNavKeydown);
        } catch (e) { /* cross-origin — ignore */ }
    }

    function init() {
        if (!syncReaderRoute({ initial: true })) return;
        void loadProviderPolicy();
        setupObservers();
        attachEpubHooks();
        setupKeyboardShortcut();
        window.addEventListener('bt:reader-route', () => syncReaderRoute());
        // Persist any pending translations if the user closes/reloads the tab.
        // pagehide covers mobile Safari and bfcache navigations where
        // beforeunload does not fire; persistCacheNow is idempotent.
        window.addEventListener('beforeunload', persistCacheNow);
        window.addEventListener('pagehide', persistCacheNow);
            // Offline-first: stop issuing network work while offline (inflight
        // requests abort into failedParagraphs, queues stay queued), then
        // clear the failure marks and resume on reconnect. Persisted
        // translations keep rendering throughout.
        window.addEventListener('offline', () => {
            isOffline = true;
            activeControllers.forEach((controller) => {
                try { controller.abort(); } catch (e) { /* already settled */ }
            });
            refreshStatus();
        });
        window.addEventListener('online', () => {
            isOffline = false;
            errorCount = 0;
            failedParagraphs.clear();
            refreshStatus();
            if (translationMode !== 'off') {
                scheduleTranslate('reconnected', { immediate: true });
            }
        });
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') persistCacheNow();
        });
        // Brief version toast helps Felix confirm the correct JS is loaded after deploys.
        setTimeout(() => showToast(`BookTranslator ${BT_UI_VERSION}`), 1200);
        if (translationMode !== 'off') {
            translateCurrentPage();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

