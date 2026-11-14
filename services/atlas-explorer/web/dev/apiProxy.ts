import http from 'node:http';
import { isIP } from 'node:net';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseEnv } from 'node:util';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin } from 'vite';

const target = 'http://127.0.0.1:8000';
const workspaceTarget = 'http://127.0.0.1:8081';
const workspacePrefix = '/__atlas/workspaces';
const workspaceApiPrefix = '/api/v1/workspaces';
const maxWorkspaceRequestBytes = 300 * 1024;
const maxWorkspaceResponseBytes = 2 * 1024 * 1024;
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

function workspaceApiPath(url: string, method: string | undefined): string | undefined {
  const [path, query] = url.split('?', 2);
  const queryKeys = query === undefined ? [] : [...new URLSearchParams(query).keys()];
  if (new Set(queryKeys).size !== queryKeys.length) return undefined;
  if (path === workspacePrefix && (method === 'GET' || method === 'POST')) {
    if (method === 'GET' && queryKeys.some((key) => !['cursor', 'limit'].includes(key))) return undefined;
    if (method === 'POST' && query !== undefined) return undefined;
    return `${workspaceApiPrefix}${query === undefined ? '' : `?${query}`}`;
  }
  const suffix = path.slice(workspacePrefix.length);
  const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const workspace = new RegExp(`^/${uuid}$`, 'i');
  const workspaceJournal = new RegExp(`^/${uuid}/journal$`, 'i');
  const workspaceMutations = new RegExp(`^/${uuid}/mutations$`, 'i');
  if (method === 'GET' && workspace.test(suffix)) {
    if (query !== undefined) return undefined;
    return `${workspaceApiPrefix}${suffix}`;
  }
  if (method === 'GET' && workspaceJournal.test(suffix)) {
    if (queryKeys.some((key) => !['after_revision', 'limit'].includes(key))) return undefined;
    return `${workspaceApiPrefix}${suffix}${query === undefined ? '' : `?${query}`}`;
  }
  if (method === 'POST' && workspaceMutations.test(suffix)) {
    if (query !== undefined) return undefined;
    return `${workspaceApiPrefix}${suffix}${query === undefined ? '' : `?${query}`}`;
  }
  return undefined;
}

function isWorkspaceNamespace(url: string): boolean {
  const path = url.split('?')[0];
  return path === workspacePrefix || path.startsWith(`${workspacePrefix}/`);
}

function reply(res: ServerResponse, status: number, message: string): void {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
  res.end(message);
}

function replyWorkspace(res: ServerResponse, status: number, code: string): void {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  const body = JSON.stringify({ code });
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  res.end(body);
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
        void handle(req, res, next).catch(() => {
          if (isWorkspaceNamespace(req.url ?? '')) replyWorkspace(res, 502, 'workspace_service_unavailable');
          else reply(res, 502, 'Dev API upstream unavailable');
        });
      });
      async function handle(req: IncomingMessage, res: ServerResponse, next: () => void) {
        const url = req.url ?? '';
        const session = url.split('?')[0] === sessionPath;
        const workspaceRoute = isWorkspaceNamespace(url);
        if (!isApiPath(url) && !session && !workspaceRoute) {
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
          if (workspaceRoute) replyWorkspace(res, 403, 'request_denied');
          else reply(res, 403, 'Dev API request denied');
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
        if (workspaceRoute) {
          const mappedPath = workspaceApiPath(url, req.method);
          if (!mappedPath || url.includes('\\')
            || req.headers.authorization !== undefined
            || req.headers['x-tenant-id'] !== undefined
            || req.headers['x-api-key'] !== undefined
            || req.headers.cookie !== undefined) {
            replyWorkspace(res, 403, 'request_denied');
            return;
          }
          const isMutation = req.method === 'POST';
          const contentType = req.headers['content-type']?.split(';')[0].trim().toLowerCase();
          const declaredLength = req.headers['content-length'];
          if (isMutation && contentType !== 'application/json') {
            replyWorkspace(res, 415, 'json_required');
            return;
          }
          if (!isMutation && declaredLength !== undefined && declaredLength !== '0') {
            replyWorkspace(res, 400, 'get_body_not_allowed');
            return;
          }
          if (declaredLength !== undefined
            && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > maxWorkspaceRequestBytes)) {
            replyWorkspace(res, 413, 'request_too_large');
            return;
          }
          const chunks: Buffer[] = [];
          let size = 0;
          let tooLarge = false;
          req.on('data', (chunk: Buffer | string) => {
            if (tooLarge) return;
            const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
            size += buffer.byteLength;
            if (size > maxWorkspaceRequestBytes) {
              tooLarge = true;
              chunks.length = 0;
              replyWorkspace(res, 413, 'request_too_large');
              return;
            }
            chunks.push(buffer);
          });
          req.on('end', () => {
            if (res.destroyed || res.headersSent) return;
            const body = Buffer.concat(chunks);
            if (!isMutation && body.byteLength > 0) {
              replyWorkspace(res, 400, 'get_body_not_allowed');
              return;
            }
            const headers: http.OutgoingHttpHeaders = {
              host: '127.0.0.1:8081',
              authorization: `Bearer ${key}`,
              'x-tenant-id': tenant,
              accept: 'application/json',
              ...(isMutation ? { 'content-type': 'application/json', 'content-length': body.byteLength } : {}),
            };
            const upstream = http.request(`${workspaceTarget}${mappedPath}`, {
              method: req.method,
              headers,
            }, (response) => {
              let responseSize = 0;
              const responseChunks: Buffer[] = [];
              response.on('data', (chunk: Buffer | string) => {
                const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
                responseSize += buffer.byteLength;
                if (responseSize > maxWorkspaceResponseBytes) {
                  upstream.destroy();
                  replyWorkspace(res, 502, 'workspace_service_unavailable');
                } else responseChunks.push(buffer);
              });
              response.on('end', () => {
                if (res.destroyed || res.headersSent) return;
                if (!response.headers['content-type']?.toLowerCase().includes('application/json')) {
                  replyWorkspace(res, 502, 'workspace_service_unavailable');
                  return;
                }
                res.writeHead(response.statusCode ?? 502, {
                  'Content-Type': 'application/json',
                  'Cache-Control': 'no-store',
                  'X-Content-Type-Options': 'nosniff',
                });
                res.end(Buffer.concat(responseChunks));
              });
              response.on('error', () => replyWorkspace(res, 502, 'workspace_service_unavailable'));
            });
            upstream.on('error', () => replyWorkspace(res, 502, 'workspace_service_unavailable'));
            req.on('aborted', () => upstream.destroy());
            res.on('close', () => upstream.destroy());
            upstream.end(body);
          });
          req.on('error', () => {
            if (!res.headersSent) replyWorkspace(res, 400, 'request_interrupted');
          });
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
