import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { AtlasPreview } from '../src/components/AtlasPreview';

afterEach(cleanup);

function click(name: string | RegExp) {
  fireEvent.click(screen.getByRole('button', { name }));
}

describe('Atlas fixture preview', () => {
  it('keeps unknown queries honest and supports an explicit sample query', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    const query = screen.getByRole('textbox', { name: /try a sample/i });
    fireEvent.change(query, { target: { value: 'something not included' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview query' }));
    expect(screen.getByRole('status')).toHaveTextContent('Preview contains samples only; live research is unavailable');
    expect(screen.queryByRole('button', { name: /Quantum Computing/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Personal computers/ })).not.toBeInTheDocument();

    fireEvent.change(query, { target: { value: 'COMPUTERS' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview query' }));
    expect(screen.getByRole('button', { name: /Personal computers/ })).toBeInTheDocument();
    expect(screen.getByText(/Fixture-only preview · local and unsaved/)).toBeInTheDocument();
  });

  it('opens adaptive facet columns and reveals sourced sample claims', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    click(/Personal computers/);
    expect(screen.getByRole('region', { name: 'Personal computers column' })).toBeInTheDocument();
    const rootColumn = screen.getByRole('region', { name: 'Starting points column' });
    expect(within(rootColumn).getByRole('button', { name: /Servers/ })).toBeInTheDocument();
    expect(within(rootColumn).getByRole('button', { name: /Supercomputers/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Security/ })).toHaveTextContent('Research needed');
    expect(screen.getByRole('button', { name: /Pricing/ })).toHaveTextContent('Unavailable');

    click(/Architecture/);
    expect(screen.getByText(/commonly combine a processor/)).toBeInTheDocument();
    expect(screen.getByText(/Sourced · unverified sample/)).toBeInTheDocument();
    expect(screen.getByText(/example\.test\/computers\/personal/)).toBeInTheDocument();

    fireEvent.click(within(rootColumn).getByRole('button', { name: /Servers/ }));
    const visited = screen.getByRole('navigation', { name: 'Visited paths' });
    fireEvent.click(within(visited).getByRole('button', { name: 'Personal computers' }));
    const computerColumn = screen.getByRole('region', { name: 'Personal computers column' });
    expect(within(computerColumn).getByRole('button', { name: /Architecture/ })).toBeInTheDocument();
  });

  it('shows conflicting evidence and source records as a distinct target type', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    click(/Servers/);
    click(/Energy use/);
    expect(screen.getByText(/Conflicting sample evidence/)).toBeInTheDocument();
    expect(screen.getByText(/Supports: Server energy note A/)).toBeInTheDocument();
    expect(screen.getByText(/Differs: Server energy note B/)).toBeInTheDocument();
    click(/Server energy note A/);
    expect(screen.getAllByRole('heading', { name: 'Server energy note A — sample' })).toHaveLength(2);
    expect(screen.getByText(/never fetched/)).toBeInTheDocument();
  });

  it('retains row focus when opening a column and returns focus to the selected parent', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    const root = screen.getByRole('region', { name: 'Starting points column' });
    const personal = within(root).getByRole('button', { name: /Personal computers/ });
    const servers = within(root).getByRole('button', { name: /Servers/ });
    personal.focus();
    fireEvent.keyDown(personal, { key: 'ArrowDown' });
    expect(servers).toHaveFocus();
    fireEvent.keyDown(servers, { key: 'ArrowRight' });
    expect(servers).toHaveFocus();
    const serverColumn = screen.getByRole('region', { name: 'Servers column' });
    const energy = within(serverColumn).getByRole('button', { name: /Energy use/ });
    energy.focus();
    fireEvent.keyDown(energy, { key: 'ArrowRight' });
    expect(energy).toHaveFocus();
    fireEvent.keyDown(energy, { key: 'ArrowLeft' });
    expect(servers).toHaveFocus();
  });

  it('labels visited paths with ancestry and scopes relationship provenance to the selected facet', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sample query choices' }), { target: { value: 'people' } });
    click('Load sample');
    click(/Avery Rowan/);
    click(/Organizations/);
    const inspector = screen.getByRole('complementary', { name: 'Evidence and details' });
    const sources = within(inspector).getByRole('region', { name: 'Sources' });
    expect(within(sources).getByText('Avery Rowan — fictional profile')).toBeInTheDocument();
    expect(within(sources).queryByText('Northstar Lab — fictional project note')).not.toBeInTheDocument();
    const connections = within(inspector).getByRole('region', { name: 'Related connections' });
    expect(within(connections).getByText(/Relationship evidence: Avery Rowan/)).toBeInTheDocument();
    expect(within(connections).queryByText('Morgan Vale')).not.toBeInTheDocument();
    const visited = screen.getByRole('navigation', { name: 'Visited paths' });
    expect(within(visited).getByRole('button', { name: 'Avery Rowan › Organizations' }))
      .toHaveTextContent('Avery Rowan › Organizations');

    const facets = screen.getByRole('region', { name: 'Avery Rowan column' });
    fireEvent.click(within(facets).getByRole('button', { name: /Biography/ }));
    expect(within(inspector).getByText('No source records for this selection.')).toBeInTheDocument();
    expect(within(inspector).queryByText(/Relationship evidence:/)).not.toBeInTheDocument();
  });

  it('switches in-memory branches, shows reused fictional identities, and offers mobile back', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sample query choices' }), { target: { value: 'people' } });
    click('Load sample');
    click(/Avery Rowan/);
    click(/Organizations/);
    click(/^EntityNorthstar Lab/);
    click(/^EntityMorgan Vale/);
    expect(screen.getByText(/Morgan Vale worked on a fictional/)).toBeInTheDocument();
    expect(screen.getByText(/Related connections/)).toBeInTheDocument();
    const visited = screen.getByRole('navigation', { name: 'Visited paths' });
    expect(within(visited).getAllByRole('button').length).toBeGreaterThan(2);
    click(/^← Back$/);
    expect(screen.getByText(/Column 4 of 4/)).toBeInTheDocument();
    fireEvent.click(within(visited).getByRole('button', { name: 'Avery Rowan' }));
    expect(screen.getByRole('region', { name: 'Avery Rowan column' })).toHaveClass('is-mobile-active');
    click(/Collaborators/);
    const collaboratorColumn = screen.getByRole('region', { name: 'Collaborators column' });
    fireEvent.click(within(collaboratorColumn).getByRole('button', { name: /Morgan Vale/ }));
    expect(screen.getByText(/Morgan Vale worked on a fictional/)).toBeInTheDocument();
  });

  it('navigates direct connection threads and recognizes an explored entity across paths', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sample query choices' }), { target: { value: 'people' } });
    click('Load sample');
    click(/Avery Rowan/);
    const inspector = screen.getByRole('complementary', { name: 'Evidence and details' });
    fireEvent.click(within(inspector).getByText('Connection view · 2 direct connections'));
    const list = within(inspector).getByRole('list', { name: 'Direct connections' });
    expect(list).toHaveTextContent('Outgoing');
    expect(list).toHaveTextContent('Avery Rowan worked alongside Morgan Vale');
    expect(list).toHaveTextContent('Suggested relationship');
    expect(list).toHaveTextContent('Sourced relationship');
    fireEvent.click(within(list).getByRole('button', { name: 'Explore Morgan Vale' }));
    expect(within(inspector).getByRole('heading', { name: 'Morgan Vale' })).toBeInTheDocument();
    expect(within(inspector).getAllByText(/Already explored/).length).toBeGreaterThan(0);

    const visited = screen.getByRole('navigation', { name: 'Visited paths' });
    fireEvent.click(within(visited).getByRole('button', { name: 'Avery Rowan' }));
    const facets = screen.getByRole('region', { name: 'Avery Rowan column' });
    fireEvent.click(within(facets).getByRole('button', { name: /Organizations/ }));
    const orgColumn = screen.getByRole('region', { name: 'Organizations column' });
    fireEvent.click(within(orgColumn).getByRole('button', { name: /Northstar Lab/ }));
    const orgList = within(inspector).getByRole('list', { name: 'Direct connections' });
    expect(orgList).toHaveTextContent('Incoming');
    expect(orgList).toHaveTextContent('Morgan Vale collaborated with Northstar Lab');
    const morganRow = within(orgList).getByRole('button', { name: 'Explore Morgan Vale' }).closest('li')!;
    expect(morganRow).toHaveTextContent('Already explored');
    fireEvent.click(within(orgList).getByRole('button', { name: 'Explore Morgan Vale' }));
    expect(within(visited).getByRole('button', {
      name: 'Avery Rowan › Organizations › Northstar Lab › Morgan Vale',
    })).toBeInTheDocument();
  });

  it('discloses children by canonical entity identity across alternate paths and connections', () => {
    render(<AtlasPreview onExit={() => undefined} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sample query choices' }), { target: { value: 'people' } });
    click('Load sample');
    click(/Avery Rowan/);
    click(/Collaborators/);
    const collaborators = screen.getByRole('region', { name: 'Collaborators column' });
    fireEvent.click(within(collaborators).getByRole('button', { name: /Morgan Vale/ }));
    const morgan = screen.getByRole('region', { name: 'Morgan Vale column' });
    expect(within(morgan).queryByText('No further sample steps here.')).not.toBeInTheDocument();
    fireEvent.click(within(morgan).getByRole('button', { name: /Career/ }));
    expect(screen.getByText(/Morgan Vale worked on a fictional/)).toBeInTheDocument();

    // Connection navigation from Morgan to Northstar Lab must disclose Northstar facets.
    const inspector = screen.getByRole('complementary', { name: 'Evidence and details' });
    const visited = screen.getByRole('navigation', { name: 'Visited paths' });
    fireEvent.click(within(visited).getByRole('button', { name: 'Avery Rowan › Collaborators › Morgan Vale' }));
    fireEvent.click(within(inspector).getByText(/Connection view/));
    fireEvent.click(within(inspector).getByRole('button', { name: 'Explore Northstar Lab' }));
    const northstar = screen.getByRole('region', { name: 'Northstar Lab column' });
    fireEvent.click(within(northstar).getByRole('button', { name: /Work/ }));
    const work = screen.getByRole('region', { name: 'Work column' });
    expect(within(work).getByRole('button', { name: /Northstar Lab — fictional project note/ })).toBeInTheDocument();
  });
});
