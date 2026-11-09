import { cleanup, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AtlasOperatorLink, independentApproverOrigin } from '../src/components/AtlasOperatorLink';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe('Independent operator navigation', () => {
  it('offers the fixed local operator link only after an exact submitter dev hint', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      role: 'submitter', developmentOnly: true,
    })));
    render(<AtlasOperatorLink batchId="batch / actual" development />);
    const link = await screen.findByRole('link');
    expect(fetch).toHaveBeenCalledWith('/__atlas/dev-session', {
      credentials: 'same-origin', headers: { Accept: 'application/json' },
    });
    const url = new URL(link.getAttribute('href')!);
    expect(url.origin).toBe('http://127.0.0.1:4318');
    expect([...url.searchParams.keys()]).toEqual(['atlasApprover', 'batch']);
    expect(url.searchParams.get('batch')).toBe('batch / actual');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText(/this link grants no authority/)).toBeInTheDocument();
  });

  it.each([
    { role: 'approver', developmentOnly: true },
    { role: 'submitter', developmentOnly: false },
    { role: 'submitter' },
  ])('does not use a non-submitter/non-development hint: %j', async hint => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(hint)));
    render(<AtlasOperatorLink batchId="batch" development />);
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('fails closed when development metadata is missing', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('Not found', { status: 404 }));
    render(<AtlasOperatorLink batchId="batch" development />);
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('has no production default or metadata fetch, but supports an operator-provided independent origin', () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    const { rerender } = render(<AtlasOperatorLink batchId="batch" development={false} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    rerender(<AtlasOperatorLink batchId="batch" development={false} configuredOrigin="https://operator.test" />);
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://operator.test/?atlasApprover=1&batch=batch');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects credentials, payload-bearing URLs, unsafe protocols and same-origin role switching', () => {
    for (const value of ['javascript:alert(1)', 'https://user:password@operator.test', 'https://operator.test/path',
      'https://operator.test/?key=secret', 'https://operator.test/#payload', 'http://remote.test', 'https://submitter.test']) {
      expect(independentApproverOrigin(value, 'https://submitter.test')).toBeNull();
    }
  });
});
