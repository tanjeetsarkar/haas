import { describe, expect, it } from 'vitest';
import { parseExplorationSnapshot, type AtlasClaim, type AtlasClaimDetails } from '../src';
import { computers } from './fixtures/explorations';

const details: AtlasClaimDetails = {
  attributes: [
    { key: 'published_price', label: 'Published price', value: 250, unit: 'USD', uncertainty: 'Synthetic fixture only' },
    { key: 'specialty', label: 'Specialty', value: ['Computing history'] },
    { key: 'availability', label: 'Availability', value: null, uncertainty: 'Not published' },
  ],
  checklist: [{ id: 'check-source', text: 'Verify the public source', required: true }],
  draft: { title: 'Inquiry draft', body: 'Informational draft; not sent.' },
};
const claim: AtlasClaim = {
  claim_id: 'details', subject_entity_id: 'computing', text: 'Synthetic presentation example.',
  observed_at: '2026-11-09T00:00:00Z',
  evidence: { status: 'unsupported', reason: 'Synthetic test only, never researched facts' },
  details,
};
const snapshot = () => ({ ...structuredClone(computers), claims: [structuredClone(claim)] });

describe('Source-scoped structured claim details', () => {
  it('preserves typed comparative attributes, published amounts, checklists and drafts', () => {
    expect(parseExplorationSnapshot(snapshot()).claims[0].details).toEqual(details);
  });
  it.each([
    { attributes: [{ key: 'amount', label: 'Amount', value: { arbitrary: 'object' } }] },
    { attributes: [{ key: 'amount', label: 'Amount', value: [1, 2] }] },
    { checklist: [{ id: 'step', text: 'Do not pretend this ran', required: true, completed: true }] },
    { draft: { title: 'Draft', body: 'Not executable', tool_name: 'send' } },
    { approval: { approved: true } },
    {},
  ])('rejects malformed or execution-state details %j', invalid => {
    const value = snapshot();
    Object.assign(value.claims[0], { details: invalid });
    expect(() => parseExplorationSnapshot(value)).toThrow('schema');
  });
  it('requires unique attribute keys and checklist IDs', () => {
    const a = snapshot();
    a.claims[0].details!.attributes!.push({ ...details.attributes![0] });
    expect(() => parseExplorationSnapshot(a)).toThrow('duplicate attribute');
    const b = snapshot();
    b.claims[0].details!.checklist!.push({ ...details.checklist![0] });
    expect(() => parseExplorationSnapshot(b)).toThrow('duplicate checklist');
  });
  it('still requires all enclosing evidence source references to exist', () => {
    const value = snapshot();
    value.claims[0].evidence = { status: 'sourced', source_ids: ['invented'] };
    expect(() => parseExplorationSnapshot(value)).toThrow('missing evidence source');
  });
});
