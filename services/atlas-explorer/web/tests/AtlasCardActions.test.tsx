import { cleanup, render, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it } from 'vitest';
import { getAtlasCardActions, type AtlasTarget } from '@haas/atlas-explorer-client';
import { AtlasCardActions, atlasActionCard } from '../src/components/AtlasCardActions';
import { testData } from './workspaceTestData';

afterEach(cleanup);

describe('Atlas card actions', () => {
  it('offers the shared inspect/custom actions for category, candidate, facet, source and unavailable cards', () => {
    const snapshot = testData().snapshot;
    snapshot.entities[0].kind = 'category';
    snapshot.entities.push({ ...snapshot.entities[1], entity_id: 'candidate', title: 'Candidate profile',
      identity_status: 'candidate' });
    snapshot.entities[0].facets.push({ key: 'unavailable', label: 'Unavailable details', scope: 'Not offered',
      status: 'unavailable', reason: 'Provider does not report this field.' });
    snapshot.claims = snapshot.claims.filter(claim => claim.subject_entity_id !== 'person');
    const targets: AtlasTarget[] = [
      { kind: 'entity', entity_id: 'subject' },
      { kind: 'entity', entity_id: 'candidate' },
      { kind: 'facet', entity_id: 'subject', facet_key: 'known' },
      { kind: 'facet', entity_id: 'subject', facet_key: 'unavailable' },
      { kind: 'source', source_id: 'source' },
      { kind: 'entity', entity_id: 'person' },
    ];
    const view = render(<>{targets.map(target => {
      const card = atlasActionCard(snapshot, target);
      return <AtlasCardActions key={card.cardId} snapshot={snapshot} target={target} drafts={{}}
        editingCardId={null} draftText="" onEdit={() => {}} onDraftText={() => {}} />;
    })}</>);
    for (const target of targets) {
      const card = atlasActionCard(snapshot, target);
      const actions = getAtlasCardActions(card);
      const region = view.getByRole('region', { name: `${card.title} actions` });
      expect(actions.some(action => action.id === 'inspect')).toBe(true);
      expect(actions.some(action => action.id === 'custom')).toBe(true);
      expect(within(region).getByRole('button', { name: 'Custom action' })).toBeInTheDocument();
      expect(within(region).getByText(/External execution unavailable — authorization release gates pending/))
        .toBeInTheDocument();
    }
    expect(within(view.getByRole('region', { name: 'Specialist actions' }))
      .getByRole('button', { name: 'Inspect missing evidence' })).toBeInTheDocument();
  });
});
