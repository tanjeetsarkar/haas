import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import App from '../src/App';

const { createBrowserAtlasAdapter } = vi.hoisted(() => ({ createBrowserAtlasAdapter: vi.fn(() => ({
  research: vi.fn(), save: vi.fn(), load: vi.fn(), recent: vi.fn(), decide: vi.fn(),
})) }));
const { createBrowserApproverAdapter } = vi.hoisted(() => ({
  createBrowserApproverAdapter: vi.fn(() => ({
    identity: vi.fn(async () => ({ principalId: 'operator', tenantId: 'tenant', roles: ['approver'] })),
    getApproval: vi.fn(), decide: vi.fn(),
  })),
}));

vi.mock('../src/liveAdapter', () => ({ createBrowserAtlasAdapter }));
vi.mock('../src/approverAdapter', () => ({ createBrowserApproverAdapter }));

afterEach(() => {
  window.history.replaceState(null, '', '/');
  createBrowserAtlasAdapter.mockClear();
  createBrowserApproverAdapter.mockClear();
  cleanup();
});

describe('Atlas preview opt-in route', () => {
  it('isolates the operator approver route from live research initialization', async () => {
    window.history.replaceState(null, '', '/?atlasApprover=1&batch=example-batch');
    render(<App />);
    expect(screen.getByRole('main', { name: 'Atlas operator approver' })).toBeInTheDocument();
    expect(createBrowserAtlasAdapter).not.toHaveBeenCalled();
    expect(createBrowserApproverAdapter).toHaveBeenCalledOnce();
    expect(await screen.findByRole('region', { name: 'Authenticated operator identity' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Approval batch id' })).toHaveValue('example-batch');
  });

  it('renders the fixture preview without constructing a HaaS client', () => {
    window.history.replaceState(null, '', '/?atlasPreview=1');
    render(<App />);
    expect(screen.getByRole('main', { name: 'Atlas fixture preview' })).toBeInTheDocument();
    expect(createBrowserAtlasAdapter).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '← Back to Atlas' })).toBeInTheDocument();
  });

  it('makes the real live workspace the default and exposes a separate sample playground', () => {
    render(<App />);
    expect(screen.getByRole('main', { name: 'Atlas research workspace' })).toBeInTheDocument();
    expect(createBrowserAtlasAdapter).toHaveBeenCalledOnce();
    expect(screen.getByRole('link', { name: 'Open sample playground' })).toHaveAttribute('href', '?atlasPreview=1');
  });
});
