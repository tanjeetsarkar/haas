import http from 'node:http';
import { isIP } from 'node:net';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseEnv } from 'node:util';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin } from 'vite';

const target = 'http://127.0.0.1:8000';
type Credentials = { HAAS_API_KEY?: string; HAAS_TENANT?: string; HAAS_APPROVER_KEY?: string };
const sessionPath = '/__atlas/dev-session';
export const approverPort = 4318;

export function readAtlasCredentials(repoRoot: string): Credentials {
  try {
    // Exactly one approved file, no process env, expansion, dotenv overlays or logging.
    const parsed = parseEnv(readFileSync(join(repoRoot, '.env.haas'), 'utf8'));
    return {
      HAAS_API_KEY: parsed.HAAS_API_KEY, HAAS_TENANT: parsed.HAAS_TENANT,
      HAAS_APPROVER_KEY: parsed.HAAS_APPROVER_KEY,
    };
  } catch {
    return {};
  }
}

// Read-only public contract check, never a forged principal or local role grant.
function principal(key: string, tenant: string): Promise<{ id: string; tenant: string } | undefined> {
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout>;
    const finish = (value?: { id: string; tenant: string }) => {
      clearTimeout(timer);
      resolve(value);
    };
    try {
      const request = http.request(`${target}/v1/me`, {
        method: 'GET', headers: { authorization: `Bearer ${key}`, 'x-tenant-id': tenant },
      }, (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk: string) => {
          body += chunk;
          if (body.length > 65_536) { finish(); request.destroy(); }
        });
        response.on('error', () => finish());
        response.on('end', () => {
          try {
            const data = JSON.parse(body);
            finish(response.statusCode === 200 && typeof data.principal_id === 'string'
              && data.principal_id.trim() && typeof data.tenant_id === 'string' && data.tenant_id.trim()
              ? { id: data.principal_id, tenant: data.tenant_id } : undefined);
          } catch { finish(); }
        });
      });
      request.on('error', () => finish());
      timer = setTimeout(() => { finish(); request.destroy(); }, 5_000);
      request.end();
    } catch { finish(); }
  });
}

function approverRoute(url: string, method: string | undefined): boolean {
  const path = url.split('?')[0];
  return method === 'GET'
    ? path === '/v1/me' || /^\/v1\/approvals(?:\/[A-Za-z0-9_-]+)?$/.test(path)
    : method === 'POST' && /^\/v1\/approvals\/[A-Za-z0-9_-]+\/decision$/.test(path);
}

export function isLoopback(address: string | undefined): boolean {
  if (!address) return false;
  if (isIP(address) === 4) return address.startsWith('127.');
  if (isIP(address) !== 6) return false;
  const normalized = address.toLowerCase();
  return normalized === '::1'
    || normalized.startsWith('::ffff:127.')
    || /^::ffff:7f[0-9a-f]{2}:[0-9a-f]{1,4}$/.test(normalized);
}

function isApiPath(url: string): boolean {
  const path = url.split('?')[0];
  return path === '/v1' || path.startsWith('/v1/');
}

function reply(res: ServerResponse, status: number, message: string): void {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
  res.end(message);
}

/** Development convenience only; never a production authentication boundary. */
export function atlasDevApiPlugins(
  command: 'serve' | 'build',
  isPreview: boolean | undefined,
  repoRoot: string,
  readCredentials: (root: string) => Credentials = readAtlasCredentials,
  mode = 'development',
): Plugin[] {
  if (command !== 'serve' || isPreview) return [];
  // Kept in this server-only closure, never in define/envPrefix or client modules.
  const credentials = readCredentials(repoRoot);
  const approver = mode === 'approver';
  const submitterKey = credentials.HAAS_API_KEY?.trim();
  const key = approver ? credentials.HAAS_APPROVER_KEY?.trim() : submitterKey;
  const tenant = credentials.HAAS_TENANT?.trim();
  const ready = Boolean(key && tenant && !/[\r\n]/.test(key + tenant)
    && (!approver || (submitterKey && submitterKey !== key && !/[\r\n]/.test(submitterKey))));

  return [{
    name: 'atlas-loopback-dev-api',
    configureServer(server) {
      // CLI --host overrides must not turn this into an authenticated LAN relay.
      if (server.config.server.host !== '127.0.0.1') {
        throw new Error('Atlas dev API requires host 127.0.0.1');
      }
      if (approver ? server.config.server.port !== approverPort : server.config.server.port === approverPort) {
        throw new Error('Atlas approver requires its separate fixed port 4318');
      }
      // API WebSocket/upgrade traffic is not a supported authentication transport.
      server.httpServer?.prependListener('upgrade', (req, socket) => {
        if (isApiPath(req.url ?? '')) {
          socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
        }
      });
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next) => {
        void handle(req, res, next).catch(() => reply(res, 502, 'Dev API upstream unavailable'));
      });
      async function handle(req: IncomingMessage, res: ServerResponse, next: () => void) {
        const url = req.url ?? '';
        const session = url.split('?')[0] === sessionPath;
        if (!isApiPath(url) && !session) {
          next();
          return;
        }
        const bound = server.httpServer?.address();
        const port = req.socket.localPort;
        const host = req.headers.host;
        const origin = req.headers.origin;
        if (!bound || typeof bound === 'string' || bound.address !== '127.0.0.1'
          || !port || port !== bound.port
          || (approver ? port !== approverPort : port === approverPort)
          || !isLoopback(req.socket.remoteAddress)
          || !isLoopback(req.socket.localAddress)
          || (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`)
          || (origin !== undefined && origin !== `http://${host}`)
          || (origin === undefined && req.method !== 'GET' && req.method !== 'HEAD')
          || req.headers['sec-fetch-site'] === 'cross-site') {
          reply(res, 403, 'Dev API request denied');
          return;
        }
        if (session) {
          if (req.method !== 'GET') { reply(res, 403, 'Dev API request denied'); return; }
          res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
          res.end(JSON.stringify({ role: approver ? 'approver' : 'submitter', developmentOnly: true }));
          return;
        }
        if (approver && !approverRoute(url, req.method)) {
          reply(res, 403, 'Dev API request denied');
          return;
        }
        if (!ready) {
          reply(res, 503, 'Dev API credentials unavailable');
          return;
        }
        if (approver) {
          const [submitter, reviewer] = await Promise.all([
            principal(submitterKey!, tenant!), principal(key!, tenant!),
          ]);
          if (!submitter || !reviewer || submitter.id === reviewer.id || submitter.tenant !== reviewer.tenant) {
            reply(res, 503, 'Dev API independent approver identity unavailable');
            return;
          }
          if (res.destroyed || req.aborted) return;
        }
        // Reject normalization escapes; never accept an absolute URL or another target.
        if (url.includes('\\') || !isApiPath(new URL(url, target).pathname)) {
          reply(res, 403, 'Dev API request denied');
          return;
        }
        const headers = { ...req.headers };
        const hopHeaders = [
          'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
          'te', 'trailer', 'transfer-encoding', 'upgrade',
          ...(req.headers.connection ?? '').split(',').map((name) => name.trim().toLowerCase()),
        ];
        for (const name of [...hopHeaders, 'authorization', 'x-tenant-id', 'x-api-key', 'cookie']) {
          delete headers[name];
        }
        headers.host = '127.0.0.1:8000';
        headers.authorization = `Bearer ${key}`;
        headers['x-tenant-id'] = tenant;
        let responseTimer: ReturnType<typeof setTimeout> | undefined;
        try {
          // Node HTTP does not follow redirects. Host, port and protocol are fixed.
          const upstream = http.request(`${target}${url}`, {
            method: req.method,
            headers,
          }, (response) => {
            clearTimeout(responseTimer);
            upstream.setTimeout(0);
            res.writeHead(response.statusCode ?? 502, response.headers);
            res.flushHeaders();
            response.on('error', () => reply(res, 502, 'Dev API upstream unavailable'));
            response.pipe(res);
          });
          upstream.on('error', () => {
            clearTimeout(responseTimer);
            reply(res, 502, 'Dev API upstream unavailable');
          });
          // Bound connection/response-header wait, not valid idle SSE streams.
          responseTimer = setTimeout(() => {
            reply(res, 502, 'Dev API upstream unavailable');
            upstream.destroy();
          }, 30_000);
          const stop = () => {
            clearTimeout(responseTimer);
            upstream.destroy();
          };
          req.on('aborted', stop);
          res.on('close', stop);
          req.pipe(upstream);
        } catch {
          clearTimeout(responseTimer);
          reply(res, 502, 'Dev API upstream unavailable');
        }
      }
    },
  }];
}
