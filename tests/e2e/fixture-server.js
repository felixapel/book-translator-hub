const http = require('http');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '../..');
const port = Number(process.env.BT_E2E_PORT || 4173);

const contentTypes = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
};

function sendFile(response, relativePath) {
    const filePath = path.join(root, relativePath);
    response.writeHead(200, {
        'Content-Type': contentTypes[path.extname(filePath)] || 'application/octet-stream',
        'Cache-Control': 'no-store',
    });
    fs.createReadStream(filePath).pipe(response);
}

function sendJson(response, status, payload) {
    response.writeHead(status, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
    });
    response.end(JSON.stringify(payload));
}

function readJsonBody(request, response, handle) {
    let raw = '';
    request.on('data', chunk => {
        raw += chunk;
        if (raw.length > 65536) request.destroy();
    });
    request.on('end', () => {
        let data = null;
        try {
            data = raw ? JSON.parse(raw) : null;
        } catch (e) {
            data = null;
        }
        if (!data || typeof data !== 'object') {
            sendJson(response, 400, { error: 'body must be a JSON object' });
            return;
        }
        handle(data);
    });
}

const server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://127.0.0.1:${port}`);

    if (url.pathname === '/bt-static/loader.js') {
        sendFile(response, 'static/loader.js');
        return;
    }
    if (url.pathname === '/bt-static/translator.js') {
        sendFile(response, 'static/translator.js');
        return;
    }
    if (url.pathname === '/bt-static/translator.css') {
        sendFile(response, 'static/translator.css');
        return;
    }
    if (url.pathname === '/bt-config.json') {
        response.writeHead(200, {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
        });
        response.end(JSON.stringify({
            apiUrl: '/bt-api',
            authMode: 'cwa_session',
            credentials: 'same-origin',
        }));
        return;
    }
    if (url.pathname === '/bt-api/glossary') {
        if (request.method === 'GET') {
            sendJson(response, 200, { entries: [] });
            return;
        }
        return readJsonBody(request, response, (data) => {
            if (request.method === 'POST'
                    && typeof data.source === 'string'
                    && typeof data.target === 'string') {
                sendJson(response, 200, {
                    entry: { source: data.source, target: data.target },
                });
                return;
            }
            if (request.method === 'DELETE'
                    && typeof data.source === 'string') {
                sendJson(response, 200, { deleted: true });
                return;
            }
            sendJson(response, 400, { error: 'bad glossary fixture call' });
        });
    }
    if (url.pathname === '/bt-api/feedback') {
        return readJsonBody(request, response, (data) => {
            if (request.method === 'POST'
                    && typeof data.para_key === 'string'
                    && (data.rating === 1 || data.rating === -1
                        || data.rating === 'up' || data.rating === 'down')) {
                const rating = data.rating === 'down' || data.rating === -1 ? -1 : 1;
                sendJson(response, 200, {
                    feedback: { para_key: data.para_key, rating },
                });
                return;
            }
            sendJson(response, 400, { error: 'bad feedback fixture call' });
        });
    }
    if (url.pathname === '/bt-api/feedback/summary') {
        sendJson(response, 200, {
            summary: { up: 0, down: 0, total: 0, score: 0 },
        });
        return;
    }
    if (url.pathname === '/bt-api/provider-policy') {
        response.writeHead(200, {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
        });
        response.end(JSON.stringify({
            primary: 'local',
            fallback: 'remote',
            generation: '0123456789abcdef0123456789abcdef',
        }));
        return;
    }
    if (url.pathname === '/chapter/1') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html><body>
          <main class="chapter">
            <p id="paragraph-one">A quiet production test paragraph.</p>
            <p id="paragraph-two">A second paragraph checks queue order.</p>
          </main>
        </body></html>`);
        return;
    }
    if (url.pathname === '/read/42') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html lang="en"><head>
          <meta charset="utf-8">
          <title>CWA reader fixture</title>
          <script src="/bt-static/loader.js?v=e2e"></script>
        </head><body>
          <main><div id="viewer"><iframe title="Book chapter" src="/chapter/1"></iframe></div></main>
        </body></html>`);
        return;
    }
    if (url.pathname === '/read/delayed') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html lang="en"><head>
          <meta charset="utf-8">
          <title>Delayed CWA reader fixture</title>
          <script src="/bt-static/loader.js?v=e2e"></script>
        </head><body>
          <main><div id="viewer"></div></main>
          <script>
            setTimeout(() => {
              const iframe = document.createElement('iframe');
              iframe.title = 'Book chapter';
              iframe.src = '/chapter/1';
              document.querySelector('#viewer').appendChild(iframe);
            }, 50);
          </script>
        </body></html>`);
        return;
    }
    if (url.pathname === '/library/7/series/42/book/99') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html lang="en"><head>
          <meta charset="utf-8">
          <title>Kavita EPUB reader fixture</title>
          <script src="/bt-static/loader.js?v=e2e"></script>
        </head><body>
          <main class="book-container">
            <div class="book-content"><p id="kavita-paragraph">A Kavita EPUB paragraph.</p></div>
          </main>
        </body></html>`);
        return;
    }
    if (url.pathname === '/library/7/series/42/manga/99') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html lang="en"><head>
          <meta charset="utf-8">
          <title>Kavita manga fixture</title>
          <script src="/bt-static/loader.js?v=e2e"></script>
        </head><body><main><canvas aria-label="manga page"></canvas></main></body></html>`);
        return;
    }
    if (url.pathname === '/library') {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end(`<!doctype html><html lang="en"><head>
          <meta charset="utf-8"><title>CWA library fixture</title>
          <script src="/bt-static/loader.js?v=e2e"></script>
        </head><body><main><h1>Library</h1></main></body></html>`);
        return;
    }
    if (url.pathname === '/favicon.ico') {
        response.writeHead(204);
        response.end();
        return;
    }

    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('not found');
});

server.listen(port, '127.0.0.1', () => {
    process.stdout.write(`fixture listening on http://127.0.0.1:${port}\n`);
});

function stop() {
    server.close(() => process.exit(0));
}

process.on('SIGINT', stop);
process.on('SIGTERM', stop);
