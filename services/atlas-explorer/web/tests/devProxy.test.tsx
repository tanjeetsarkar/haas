// @vitest-environment node
import http from 'node:http';
import { PassThrough, Writable } from 'node:stream';
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { connect } from 'node:net';
import { once } from 'node:events';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { createServer, preview, type ViteDevServer } from 'vite';
import { approverPort, atlasDevApiPlugins, isLoopback, readAtlasCredentials } from '../dev/apiProxy';
import config from '../vite.config';

// Synthetic credentials only; never load the developer's environment in tests.
const env = { HAAS_API_KEY: 'test-only-key', HAAS_TENANT: 'test-only-tenant' };
const dualEnv = { ...env, HAAS_APPROVER_KEY: 'test-only-approver-key' };
const directRequest = http.request;
type Middleware = (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
const cleanup: Array<() => void | Promise<void>> = [];

function harness(
  credentials: { HAAS_API_KEY?: string; HAAS_TENANT?: string; HAAS_APPROVER_KEY?: string } = env,
  boundHost = '127.0.0.1',
  configuredHost = '127.0.0.1',
  mode = 'development',
  configuredPort = mode === 'approver' ? approverPort : 4321,
) {
  const port = mode === 'approver' ? approverPort : 4321;
  let middleware: Middleware;
  const readCredentials = vi.fn(() => credentials);
  const [plugin] = atlasDevApiPlugins('serve', false, '/synthetic-root', readCredentials, mode);
  const server = {
    config: { server: { host: configuredHost, port: configuredPort } },
    httpServer: {
      address: () => ({ address: boundHost, port }),
      prependListener: vi.fn(),
    },
    middlewares: { use: (fn: Middleware) => { middleware = fn; } },
  } as unknown as ViteDevServer;
  const hook = plugin.configureServer;
  if (typeof hook !== 'function') throw new Error('Missing dev middleware');
  hook.call({} as never, server);
  return {
    readCredentials,
    send(overrides: {
      remote?: string; local?: string; port?: number; url?: string; method?: string; body?: string;
      headers?: Record<string, string | undefined>;
    } = {}) {
      const req = Object.assign(new PassThrough(), {
        url: overrides.url ?? '/v1/runs',
        method: overrides.method ?? 'GET',
        headers: { host: `127.0.0.1:${port}`, ...overrides.headers },
        socket: {
          remoteAddress: overrides.remote ?? '127.0.0.1',
          localAddress: overrides.local ?? '127.0.0.1',
          localPort: overrides.port ?? port,
        },
      }) as unknown as IncomingMessage;
      let body = '';
      const res = Object.assign(new Writable({
        write(chunk, _encoding, done) { body += chunk.toString(); done(); },
      }), {
        statusCode: 0,
        responseHeaders: {} as Record<string, string>,
        headersSent: false,
        flushHeaders: vi.fn(),
        writeHead(status: number, headers: Record<string, string> = {}) {
          this.statusCode = status; this.responseHeaders = headers; this.headersSent = true;
        },
      });
      const next = vi.fn();
      cleanup.push(() => { res.destroy(); });
      middleware!(req, res as unknown as ServerResponse, next);
      req.end(overrides.body);
      return { req, res, next, body: () => body };
    },
  };
}

function mockUpstream() {
  const upstream = Object.assign(new PassThrough(), { setTimeout: vi.fn() });
  const request = vi.spyOn(http, 'request').mockReturnValue(upstream as unknown as http.ClientRequest);
  return { upstream, request };
}

afterEach(async () => {
  for (const close of cleanup.splice(0).reverse()) await close();
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('dev-only configuration', () => {
  it.each([['build', false], ['serve', true]] as const)('does not read credentials for %s preview=%s', (command, preview) => {
    const readEnv = vi.fn();
    expect(atlasDevApiPlugins(command, preview, '/unused', readEnv)).toEqual([]);
    expect(readEnv).not.toHaveBeenCalled();
  });

  it('uses the exact server-only credential reader contract', () => {
    expect(harness().readCredentials).toHaveBeenCalledWith('/synthetic-root');
  });

  it('parses only root .env.haas without expansion, environment mutation or debug output', () => {
    const root = mkdtempSync(join(tmpdir(), 'atlas-credentials-'));
    cleanup.push(() => rmSync(root, { recursive: true, force: true }));
    for (const file of ['.env', '.env.local', '.env.haas.local']) {
      writeFileSync(join(root, file), 'HAAS_API_KEY=wrong-key\nHAAS_TENANT=wrong-tenant\n');
    }
    vi.stubEnv('HAAS_API_KEY', 'wrong-process-key');
    vi.stubEnv('HAAS_TENANT', 'wrong-process-tenant');
    vi.stubEnv('DEBUG', 'vite:env');
    const stdout = vi.spyOn(process.stdout, 'write').mockReturnValue(true);
    const stderr = vi.spyOn(process.stderr, 'write').mockReturnValue(true);
    writeFileSync(join(root, '.env.haas'), 'export HAAS_API_KEY="test-only-key" # comment\nHAAS_TENANT=\'test-only-tenant\'\n');
    const before = { ...process.env };
    expect(readAtlasCredentials(root)).toEqual(env);
    expect(process.env).toEqual(before);
    expect(stdout).not.toHaveBeenCalled();
    expect(stderr).not.toHaveBeenCalled();
    writeFileSync(join(root, '.env.haas'), 'HAAS_TENANT=${HAAS_TENANT}\n');
    expect(readAtlasCredentials(root)).toEqual({ HAAS_API_KEY: undefined, HAAS_TENANT: '${HAAS_TENANT}' });
    rmSync(join(root, '.env.haas'));
    expect(readAtlasCredentials(root)).toEqual({});
  });

  it('binds strictly to loopback with explicit hosts and no preview proxy', async () => {
    if (typeof config !== 'function') throw new Error('Expected config function');
    const result = await config({ command: 'build', mode: 'production', isPreview: false });
    expect(result.server).toMatchObject({
      host: '127.0.0.1', strictPort: true, allowedHosts: ['127.0.0.1', 'localhost'], cors: false,
    });
    expect(result.server?.proxy).toBeUndefined();
    expect(result.preview?.proxy).toBeUndefined();
  });

  it.each(['0.0.0.0', 'localhost', '::', true])('refuses host override %s at startup', (host) => {
    expect(() => harness(env, '127.0.0.1', host as string)).toThrow('requires host 127.0.0.1');
  });

  it('reserves the approver port for the explicit operator mode', async () => {
    expect(() => harness(dualEnv, '127.0.0.1', '127.0.0.1', 'approver', 3000)).toThrow('separate fixed port');
    expect(() => harness(dualEnv, '127.0.0.1', '127.0.0.1', 'development', approverPort)).toThrow('separate fixed port');
    if (typeof config !== 'function') throw new Error('Expected config function');
    const result = await config({ command: 'build', mode: 'approver' });
    expect(result.server?.port).toBe(approverPort);
    expect((result.plugins ?? []).flat().some((plugin) => plugin && 'name' in plugin && plugin.name === 'atlas-loopback-dev-api')).toBe(false);
  });
});

describe('loopback and browser request guards', () => {
  it.each(['127.0.0.1', '127.2.3.4', '::1', '::ffff:127.0.0.1', '::ffff:7f00:1'])('recognizes %s', (address) => {
    expect(isLoopback(address)).toBe(true);
  });
  it.each([undefined, '', '127.evil', '128.0.0.1', '::ffff:192.168.1.1', '::ffff:7e00:1', 'fe80::1'])('rejects source %s', (address) => {
    expect(isLoopback(address)).toBe(false);
  });

  it.each([
    { remote: '10.0.0.2' },
    { remote: '::ffff:192.168.0.2' },
    { local: '10.0.0.1' },
    { port: 3000 },
    { headers: { host: undefined } },
    { headers: { host: 'evil.example:4321' } },
    { headers: { host: '127.0.0.1.evil:4321' } },
    { headers: { host: 'localhost:3000' } },
    { headers: { host: 'localhost' } },
    { headers: { origin: 'http://evil.example:4321' } },
    { headers: { origin: 'http://localhost:4321' } },
    { headers: { origin: 'http://127.0.0.1:3000' } },
    { headers: { origin: 'https://127.0.0.1:4321' } },
    { headers: { origin: 'null' } },
    { headers: { 'sec-fetch-site': 'cross-site' } },
  ])('denies untrusted request %# without touching upstream', (overrides) => {
    const { request } = mockUpstream();
    const result = harness().send(overrides);
    expect(result.res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['POST', 'OPTIONS'])('rejects cross-origin %s', (method) => {
    const { request } = mockUpstream();
    expect(harness().send({ method, headers: { origin: 'http://attacker.example' } }).res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])('rejects no-Origin %s', (method) => {
    const { request } = mockUpstream();
    expect(harness().send({ method }).res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['GET', 'HEAD'])('permits no-Origin loopback %s', (method) => {
    const { request } = mockUpstream();
    harness().send({ method });
    expect(request).toHaveBeenCalledOnce();
  });

  it('rejects a wildcard actual listener even with a loopback socket', () => {
    const { request } = mockUpstream();
    expect(harness(env, '0.0.0.0').send().res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    {}, { HAAS_TENANT: 'tenant' }, { HAAS_API_KEY: 'key' },
    { HAAS_API_KEY: '', HAAS_TENANT: 'tenant' },
    { HAAS_API_KEY: 'key', HAAS_TENANT: '' },
    { HAAS_API_KEY: ' \t ', HAAS_TENANT: 'tenant' },
    { HAAS_API_KEY: 'key\r\ninjected: value', HAAS_TENANT: 'tenant' },
  ])('fails closed without valid credentials', (credentials) => {
    const { request } = mockUpstream();
    const result = harness(credentials).send();
    expect(result.res.statusCode).toBe(503);
    expect(result.body()).toBe('Dev API credentials unavailable');
    expect(request).not.toHaveBeenCalled();
  });
});

describe('integrated mocked proxy middleware', () => {
  it('maps only workspace routes to the fixed service with server-side identity', async () => {
    const { request, upstream } = mockUpstream();
    const result = harness().send({
      url: '/__atlas/workspaces?limit=20', method: 'GET',
      headers: { origin: 'http://127.0.0.1:4321', authorization: undefined, 'x-tenant-id': undefined },
    });
    await new Promise((resolve) => setImmediate(resolve));
    expect(request).toHaveBeenCalledWith('http://127.0.0.1:8081/api/v1/workspaces?limit=20', {
      method: 'GET',
      headers: expect.objectContaining({
        host: '127.0.0.1:8081',
        authorization: `Bearer ${env.HAAS_API_KEY}`,
        'x-tenant-id': env.HAAS_TENANT,
        accept: 'application/json',
      }),
    }, expect.any(Function));
    expect(upstream.read()).toBeNull();
    expect(result.res.statusCode).toBe(0);
  });

  it.each(['authorization', 'x-tenant-id', 'x-api-key', 'cookie'])(
    'rejects browser identity header %s on workspace path',
    (header) => {
      const { request } = mockUpstream();
      const result = harness().send({
        url: '/__atlas/workspaces', method: 'POST',
        headers: { origin: 'http://127.0.0.1:4321', [header]: 'forged' },
        body: '{"schema_version":1}',
      });
      expect(result.res.statusCode).toBe(403);
      expect(request).not.toHaveBeenCalled();
    },
  );

  it.each([
    ['/__atlas/workspaces/../v1/me', 'GET'],
    ['/__atlas/workspaces/not-a-uuid', 'GET'],
    ['/__atlas/workspaces/00000000-0000-0000-0000-000000000001/mutations', 'GET'],
    ['/__atlas/workspaces?unexpected=1', 'GET'],
    ['/__atlas/workspaces?cursor=a&cursor=b', 'GET'],
    ['/__atlas/workspaces/00000000-0000-0000-0000-000000000001', 'DELETE'],
  ])('rejects unsupported workspace path or method %s %s', (url, method) => {
    const { request } = mockUpstream();
    const result = harness().send({ url, method, headers: { origin: 'http://127.0.0.1:4321' } });
    expect(result.res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it('enforces JSON bodies on mutation routes', () => {
    const { request } = mockUpstream();
    const result = harness().send({
      url: '/__atlas/workspaces', method: 'POST',
      headers: { origin: 'http://127.0.0.1:4321', 'content-type': 'text/plain' },
      body: 'not json',
    });
    expect(result.res.statusCode).toBe(415);
    expect(request).not.toHaveBeenCalled();
  });

  it('uses JSON errors and never forwards upstream Set-Cookie on workspace routes', async () => {
    const { request } = mockUpstream();
    const denied = harness().send({
      url: '/__atlas/workspaces', method: 'GET',
      headers: { origin: 'http://attacker.example' },
    });
    expect(denied.res.statusCode).toBe(403);
    expect(JSON.parse(denied.body())).toEqual({ code: 'request_denied' });
    expect(request).not.toHaveBeenCalled();

    const allowed = harness().send({
      url: '/__atlas/workspaces', method: 'GET',
      headers: { origin: 'http://127.0.0.1:4321' },
    });
    await new Promise((resolve) => setImmediate(resolve));
    const response = Object.assign(new PassThrough(), {
      statusCode: 200,
      headers: { 'content-type': 'application/json', 'set-cookie': 'private=session' },
    });
    const onResponse = request.mock.calls[0][2] as (res: IncomingMessage) => void;
    onResponse(response as unknown as IncomingMessage);
    const responseEnded = once(response, 'end');
    response.end('{"items":[]}');
    await responseEnded;
    expect(allowed.res.statusCode).toBe(200);
    expect(allowed.body()).toBe('{"items":[]}');
    expect(allowed.res.responseHeaders).not.toHaveProperty('Set-Cookie');
    expect(allowed.res.responseHeaders).toMatchObject({
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
    });
  });

  it('rejects oversized declared workspace request before upstream', () => {
    const { request } = mockUpstream();
    const result = harness().send({
      url: '/__atlas/workspaces', method: 'POST',
      headers: {
        origin: 'http://127.0.0.1:4321',
        'content-type': 'application/json',
        'content-length': String(300 * 1024 + 1),
      },
    });
    expect(result.res.statusCode).toBe(413);
    expect(JSON.parse(result.body())).toEqual({ code: 'request_too_large' });
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['/v1', '/v1?limit=1', '/v1/runs', '/v1/threads', '/v1/artifacts', '/v1/approvals', '/v1/me'])('forwards broad public API path %s with server auth only', (url) => {
    const { request } = mockUpstream();
    harness().send({
      url, remote: '::ffff:127.0.0.1', method: 'POST',
      headers: {
        host: 'localhost:4321', origin: 'http://localhost:4321', 'sec-fetch-site': 'same-origin',
        authorization: 'untrusted', 'x-tenant-id': 'untrusted', 'x-api-key': 'untrusted',
        'proxy-authorization': 'untrusted', connection: 'x-tenant-id',
      },
    });
    expect(request).toHaveBeenCalledWith(`http://127.0.0.1:8000${url}`, {
      method: 'POST',
      headers: expect.objectContaining({
        host: '127.0.0.1:8000', authorization: `Bearer ${env.HAAS_API_KEY}`,
        'x-tenant-id': env.HAAS_TENANT,
      }),
    }, expect.any(Function));
    const headers = request.mock.calls[0][1]!.headers;
    expect(headers).not.toHaveProperty('x-api-key');
    expect(headers).not.toHaveProperty('proxy-authorization');
    expect(headers).not.toHaveProperty('connection');
  });

  it.each(['/v10', '/v1evil', '/v1.evil', '//attacker/v1', 'http://attacker/v1'])('never proxies %s', (url) => {
    const { request } = mockUpstream();
    expect(harness().send({ url }).next).toHaveBeenCalledOnce();
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['/v1/../admin', '/v1/%2e%2e/admin', '/v1/\\attacker'])('rejects normalization escape %s', (url) => {
    const { request } = mockUpstream();
    expect(harness().send({ url }).res.statusCode).toBe(403);
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['event', 'throw'])('returns a safe 502 on upstream %s failure without logging', (mode) => {
    const { request, upstream } = mockUpstream();
    const errorLog = vi.spyOn(console, 'error');
    const warnLog = vi.spyOn(console, 'warn');
    const failure = new Error(`synthetic sensitive upstream details ${env.HAAS_API_KEY}`);
    if (mode === 'throw') request.mockImplementation(() => { throw failure; });
    const result = harness().send();
    if (mode === 'event') upstream.emit('error', failure);
    expect(result.res.statusCode).toBe(502);
    expect(result.body()).toBe('Dev API upstream unavailable');
    expect(errorLog).not.toHaveBeenCalled();
    expect(warnLog).not.toHaveBeenCalled();
  });

  it('streams request bodies without requiring browser authentication', () => {
    const { upstream, request } = mockUpstream();
    harness().send({ method: 'POST', headers: { origin: 'http://127.0.0.1:4321' }, body: '{"input":"test"}' });
    expect(upstream.read()?.toString()).toBe('{"input":"test"}');
    expect(request).toHaveBeenCalledOnce();
  });

  it('returns a safe 502 on upstream timeout', () => {
    vi.useFakeTimers();
    const { upstream } = mockUpstream();
    const result = harness().send();
    vi.advanceTimersByTime(30_000);
    expect(result.res.statusCode).toBe(502);
    expect(result.body()).toBe('Dev API upstream unavailable');
    expect(upstream.destroyed).toBe(true);
  });

  it('does not time out an idle stream after response headers', () => {
    vi.useFakeTimers();
    const { request, upstream } = mockUpstream();
    const result = harness().send();
    const response = Object.assign(new PassThrough(), {
      statusCode: 200, headers: { 'content-type': 'text/event-stream' },
    });
    const onResponse = request.mock.calls[0][2] as (res: IncomingMessage) => void;
    onResponse(response as unknown as IncomingMessage);
    vi.advanceTimersByTime(120_000);
    expect(upstream.destroyed).toBe(false);
    expect(upstream.setTimeout).toHaveBeenCalledWith(0);
    response.end('data: alive\n\n');
    expect(result.res.statusCode).toBe(200);
    expect(result.body()).toBe('data: alive\n\n');
  });

  it('streams upstream responses but never follows a redirect', () => {
    const { request } = mockUpstream();
    const result = harness().send();
    const response = Object.assign(new PassThrough(), {
      statusCode: 302, headers: { location: 'http://untrusted.example/v1' },
    });
    const onResponse = request.mock.calls[0][2] as (res: IncomingMessage) => void;
    onResponse(response as unknown as IncomingMessage);
    response.end('redirect');
    expect(result.res.statusCode).toBe(302);
    expect(result.body()).toBe('redirect');
    expect(request).toHaveBeenCalledOnce();
  });
});

async function listeningFixture(options: {
  mode?: string; credentials?: typeof dualEnv | typeof env;
  samePrincipal?: boolean; foreignTenant?: boolean; identityFailure?: boolean;
} = {}) {
  const credentials = options.credentials ?? dualEnv;
  const mode = options.mode ?? 'development';
  const root = mkdtempSync(join(tmpdir(), 'atlas-vite-'));
  cleanup.push(() => rmSync(root, { recursive: true, force: true }));
  writeFileSync(join(root, '.env.haas'), Object.entries(credentials).map(([name, value]) => `${name}=${value}`).join('\n'));
  mkdirSync(join(root, 'dist'));
  writeFileSync(join(root, 'dist/index.html'), '<html>preview fixture</html>');
  const received: Array<{ url?: string; headers: http.IncomingHttpHeaders; body: string }> = [];
  const forwarded: http.ClientRequest[] = [];
  let upgrades = 0;
  const upstream = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      received.push({ url: req.url, headers: req.headers, body });
      if (req.url === '/v1/me') {
        const approver = req.headers.authorization === `Bearer ${dualEnv.HAAS_APPROVER_KEY}`;
        res.writeHead(options.identityFailure ? 401 : 200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          principal_id: approver && !options.samePrincipal ? 'reviewer-principal' : 'submitter-principal',
          tenant_id: approver && options.foreignTenant ? 'foreign-tenant' : 'fixture-tenant-id',
          roles: approver ? ['approver'] : ['member'],
        }));
        return;
      }
      if (req.url === '/v1/events') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.flushHeaders();
        return;
      }
      if (req.url === '/v1/failure') {
        req.socket.destroy();
        return;
      }
      res.writeHead(req.url === '/v1/approvals' ? 403 : 200);
      res.end('upstream fixture');
    });
  });
  upstream.on('upgrade', (_req, socket) => { upgrades++; socket.destroy(); });
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  cleanup.push(() => new Promise<void>((resolve) => {
    upstream.closeAllConnections();
    upstream.close(() => resolve());
  }));
  const upstreamAddress = upstream.address();
  if (!upstreamAddress || typeof upstreamAddress === 'string') throw new Error('Missing fixture listener');
  // Keep the production target fixed. Remap only its transport to an ephemeral
  // real HTTP fixture, avoiding interference with the user's Docker port 8000.
  const originalRequest = http.request;
  vi.spyOn(http, 'request').mockImplementation(((url: string, options: http.RequestOptions, callback: (res: IncomingMessage) => void) => {
    expect(new URL(url).origin).toBe('http://127.0.0.1:8000');
    const mapped = new URL(url);
    mapped.port = String(upstreamAddress.port);
    const request = originalRequest(mapped, options, callback);
    forwarded.push(request);
    return request;
  }) as typeof http.request);
  const base = {
    configFile: false as const, envFile: false as const, root, logLevel: 'silent' as const,
    optimizeDeps: { noDiscovery: true },
    server: { host: '127.0.0.1', port: mode === 'approver' ? approverPort : 0, strictPort: true, allowedHosts: ['127.0.0.1', 'localhost'], cors: false },
  };
  const dev = await createServer({ ...base, plugins: atlasDevApiPlugins('serve', false, root, undefined, mode) });
  cleanup.push(() => dev.close());
  await dev.listen();
  const address = dev.httpServer!.address();
  if (!address || typeof address === 'string') throw new Error('Missing dev listener');
  return { root, base, dev, port: address.port, received, forwarded, upgrades: () => upgrades };
}

function realRequest(port: number, options: http.RequestOptions = {}, body = '') {
  return new Promise<{ status: number; body: string }>((resolve, reject) => {
    // Captured transport leaves only upstream forwarding remapped.
    const req = directRequest({
      hostname: '127.0.0.1', port, path: '/v1/me', agent: false, ...options,
    }, (res) => {
      let text = '';
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => resolve({ status: res.statusCode!, body: text }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

describe('listening HTTP / Vite integration', () => {
  it('rejects hostile requests before any upstream call; forwards same-origin writes with server identity', async () => {
    const fixture = await listeningFixture();
    const { port, received } = fixture;
    for (const options of [
      { headers: { host: 'attacker.example' } },
      { headers: { host: 'localhost:1' } },
      { headers: { origin: `http://localhost:${port}` } },
      { headers: { origin: 'null' } },
      { headers: { 'sec-fetch-site': 'cross-site' } },
      { method: 'POST' },
      { method: 'OPTIONS' },
      { method: 'POST', headers: { origin: 'http://attacker.example' } },
      { method: 'OPTIONS', headers: { origin: 'http://attacker.example' } },
    ]) {
      expect((await realRequest(port, options)).status).toBe(403);
    }
    expect(received).toHaveLength(0);
    const result = await realRequest(port, {
      method: 'POST', path: '/v1/runs',
      headers: {
        origin: `http://127.0.0.1:${port}`, authorization: 'untrusted',
        'x-tenant-id': 'untrusted', 'x-api-key': 'untrusted', 'content-length': '4',
      },
    }, 'body');
    expect(result).toEqual({ status: 200, body: 'upstream fixture' });
    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({
      url: '/v1/runs', body: 'body',
      headers: { authorization: `Bearer ${env.HAAS_API_KEY}`, 'x-tenant-id': env.HAAS_TENANT },
    });
    expect(received[0].headers).not.toHaveProperty('x-api-key');
    expect((await realRequest(port, { path: '/v1/approvals' })).status).toBe(403);
    expect(await realRequest(port, { path: '/v1/failure' })).toEqual({
      status: 502, body: 'Dev API upstream unavailable',
    });
  });

  describe('independent operator-launched approver session', () => {
    it('never switches to the approver key in the normal workspace, even for decisions', async () => {
      const { port, received } = await listeningFixture();
      const metadata = await realRequest(port, { path: '/__atlas/dev-session' });
      expect(JSON.parse(metadata.body)).toEqual({ role: 'submitter', developmentOnly: true });
      const me = await realRequest(port);
      expect(JSON.parse(me.body).principal_id).toBe('submitter-principal');
      await realRequest(port, {
        method: 'POST', path: '/v1/approvals/batch-1/decision',
        headers: { origin: `http://127.0.0.1:${port}`, 'x-atlas-role': 'approver' },
      });
      expect(received).toHaveLength(2);
      expect(received.every((request) => request.headers.authorization === `Bearer ${env.HAAS_API_KEY}`)).toBe(true);
    });

    it('rejects disallowed routes and submitter-origin requests before touching either identity', async () => {
      const { port, received } = await listeningFixture({ mode: 'approver' });
      expect(port).toBe(approverPort);
      for (const options of [
        { method: 'POST', path: '/v1/runs' },
        { method: 'POST', path: '/v1/artifacts' },
        { method: 'GET', path: '/v1/runs' },
        { method: 'POST', path: '/v1/approvals' },
        { method: 'POST', path: '/v1/approvals/batch-1/decision/extra' },
        { method: 'DELETE', path: '/v1/approvals/batch-1' },
        { method: 'POST', path: '/v1/approvals/batch-1/decision', headers: {} },
        { method: 'POST', path: '/v1/approvals/batch-1/decision', headers: { origin: 'http://127.0.0.1:3000' } },
        { method: 'POST', path: '/v1/approvals/batch-1/decision', headers: { origin: `http://localhost:${port}` } },
        { method: 'GET', path: '/v1/me', headers: { origin: 'http://127.0.0.1:3000' } },
        { method: 'GET', path: '/v1/me', headers: { host: '127.0.0.1:3000' } },
        { method: 'GET', path: '/v1/me', headers: { 'sec-fetch-site': 'cross-site' } },
        { method: 'OPTIONS', path: '/v1/approvals/batch-1/decision' },
        { method: 'POST', path: '/v1/approvals/batch-1/../decision' },
      ]) {
        const request = {
          ...options,
          headers: { origin: `http://127.0.0.1:${port}`, ...options.headers },
        };
        if (options.headers && Object.keys(options.headers).length === 0) request.headers = {};
        expect((await realRequest(port, request)).status).toBe(403);
      }
      expect(received).toHaveLength(0);
      const metadata = await realRequest(port, { path: '/__atlas/dev-session' });
      expect(JSON.parse(metadata.body)).toEqual({ role: 'approver', developmentOnly: true });
      expect(received).toHaveLength(0);
    });

    it('checks real /me principals and forwards only the restricted operations using the approver key', async () => {
      const { port, received } = await listeningFixture({ mode: 'approver' });
      const me = await realRequest(port);
      expect(JSON.parse(me.body)).toEqual({
        principal_id: 'reviewer-principal', tenant_id: 'fixture-tenant-id', roles: ['approver'],
      });
      expect(received.map((request) => request.url)).toEqual(['/v1/me', '/v1/me', '/v1/me']);
      expect(received.map((request) => request.headers.authorization)).toEqual([
        `Bearer ${env.HAAS_API_KEY}`, `Bearer ${dualEnv.HAAS_APPROVER_KEY}`, `Bearer ${dualEnv.HAAS_APPROVER_KEY}`,
      ]);
      for (const path of ['/v1/approvals', '/v1/approvals/batch-1']) {
        await realRequest(port, { path });
      }
      const decision = await realRequest(port, {
        method: 'POST', path: '/v1/approvals/batch-1/decision',
        headers: {
          origin: `http://127.0.0.1:${port}`, authorization: 'untrusted',
          'x-tenant-id': 'untrusted', 'content-length': '21',
        },
      }, '{"decision":"reject"}');
      expect(decision.status).toBe(200);
      expect(received.at(-1)).toMatchObject({
        url: '/v1/approvals/batch-1/decision', body: '{"decision":"reject"}',
        headers: { authorization: `Bearer ${dualEnv.HAAS_APPROVER_KEY}`, 'x-tenant-id': env.HAAS_TENANT },
      });
      expect(received.filter((request) => request.url !== '/v1/me').every(
        (request) => request.headers.authorization === `Bearer ${dualEnv.HAAS_APPROVER_KEY}`,
      )).toBe(true);
    });

    it.each([
      { credentials: env },
      { credentials: { ...dualEnv, HAAS_APPROVER_KEY: '' } },
      { credentials: { ...dualEnv, HAAS_APPROVER_KEY: env.HAAS_API_KEY } },
    ])('fails closed without a distinct valid approver key %#', async (options) => {
      const { port, received } = await listeningFixture({ mode: 'approver', ...options });
      expect((await realRequest(port)).status).toBe(503);
      expect(received).toHaveLength(0);
    });

    it.each([
      { samePrincipal: true },
      { foreignTenant: true },
      { identityFailure: true },
    ])('does not equate different keys with independent authority %#', async (options) => {
      const { port, received } = await listeningFixture({ mode: 'approver', ...options });
      const result = await realRequest(port, {
        method: 'POST', path: '/v1/approvals/batch-1/decision',
        headers: { origin: `http://127.0.0.1:${port}` },
      });
      expect(result).toEqual({ status: 503, body: 'Dev API independent approver identity unavailable' });
      expect(received).toHaveLength(2);
      expect(received.every((request) => request.url === '/v1/me')).toBe(true);
    });

    it.each([['build', false], ['serve', true]] as const)('does not read either identity in approver %s preview=%s', (command, isPreview) => {
      const reader = vi.fn(() => dualEnv);
      expect(atlasDevApiPlugins(command, isPreview, '/unused', reader, 'approver')).toEqual([]);
      expect(reader).not.toHaveBeenCalled();
    });
  });

  it('streams real SSE headers and disconnects without an idle socket timeout', async () => {
    const { port, forwarded } = await listeningFixture();
    const req = http.get({ hostname: '127.0.0.1', port, path: '/v1/events' });
    cleanup.push(() => { req.destroy(); });
    const [response] = await once(req, 'response') as [IncomingMessage];
    expect(response.statusCode).toBe(200);
    expect(response.headers['content-type']).toBe('text/event-stream');
    expect(forwarded[0].socket!.timeout ?? 0).toBe(0);
    response.destroy();
  });

  it('does not broker authenticated API WebSocket upgrades', async () => {
    const fixture = await listeningFixture();
    const socket = connect(fixture.port, '127.0.0.1');
    cleanup.push(() => { socket.destroy(); });
    await once(socket, 'connect');
    socket.write(
      `GET /v1/events HTTP/1.1\r\nHost: 127.0.0.1:${fixture.port}\r\n`
      + `Origin: http://127.0.0.1:${fixture.port}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n`
      + 'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n',
    );
    let response = '';
    socket.on('data', (chunk) => { response += chunk; });
    await once(socket, 'end');
    expect(response).toContain('403 Forbidden');
    expect(fixture.received).toHaveLength(0);
    expect(fixture.upgrades()).toBe(0);
  });

  it('preview listens without reading credentials or forwarding any API requests', async () => {
    const fixture = await listeningFixture();
    const readCredentials = vi.fn(() => env);
    const server = await preview({
      ...fixture.base,
      plugins: atlasDevApiPlugins('serve', true, fixture.root, readCredentials),
      preview: { host: '127.0.0.1', port: 0, strictPort: true },
    });
    cleanup.push(() => new Promise<void>((resolve) => server.httpServer.close(() => resolve())));
    const address = server.httpServer.address();
    if (!address || typeof address === 'string') throw new Error('Missing preview listener');
    const result = await realRequest(address.port);
    expect(result.body).not.toBe('upstream fixture');
    const upgrade = await realRequest(address.port, {
      headers: {
        connection: 'Upgrade', upgrade: 'websocket',
        'sec-websocket-version': '13', 'sec-websocket-key': 'dGhlIHNhbXBsZSBub25jZQ==',
      },
    });
    expect(upgrade.status).not.toBe(101);
    expect(readCredentials).not.toHaveBeenCalled();
    expect(fixture.received).toHaveLength(0);
    expect(fixture.upgrades()).toBe(0);
  });
});
