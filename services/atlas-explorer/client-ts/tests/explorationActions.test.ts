import { describe, expect, it } from 'vitest';
import {
  getAtlasCardActions, parseAtlasActionDraft, runAtlasLocalCardAction,
  type AtlasActionCard, type AtlasActionDraftV1, type AtlasActionCardKind,
} from '../src/explorationActions';

const card = (kind: AtlasActionCardKind, evidenceRefs: string[] = []): AtlasActionCard => ({
  cardId: `card-${kind}`, kind, title: '<Untrusted card>',
  workspaceRevision: 'workspace-4', branchRevision: null, evidenceRefs,
  sourcedFacts: [{ label: 'Public detail', value: '<script> inert\n## injected heading',
    evidenceRefs: ['source-a'] }],
  missingFields: ['Unknown availability'],
});

describe('trusted deterministic Atlas card action catalog', () => {
  it.each(['entity', 'category', 'facet', 'source', 'candidate', 'unavailable'] as const)(
    'offers safe inspection and Custom action for %s cards', kind => {
      const actions = getAtlasCardActions(card(kind));
      expect(actions.map(action => action.id)).toEqual(['inspect', 'custom']);
      expect(actions[0]).toMatchObject({
        label: 'Inspect missing evidence', kind: 'local', operation: 'inspect_missing_evidence',
      });
      expect(actions[1]).toMatchObject({
        label: 'Custom action', kind: 'custom', externalExecution: 'unavailable',
      });
    });

  it('offers source inspection and export only for cited facts', () => {
    const actions = getAtlasCardActions(card('entity', ['source-a']));
    expect(actions.map(action => action.id)).toEqual(['inspect', 'export', 'custom']);
    expect(actions[0]).toMatchObject({ label: 'Inspect sources', operation: 'inspect_sources' });
    expect(getAtlasCardActions({ ...card('entity', ['source-a']), sourcedFacts: [
      { label: 'Uncited', value: 'not grounded', evidenceRefs: ['other-source'] },
    ] }).map(action => action.id)).toEqual(['inspect', 'custom']);
    expect(getAtlasCardActions(card('source', ['https://outside.example'])).map(action => action.id))
      .toEqual(['inspect', 'custom']);
  });

  it('returns a local inspection result and refuses to execute Custom action', () => {
    expect(runAtlasLocalCardAction(card('candidate'), 'inspect')).toEqual({
      type: 'inspection', cardId: 'card-candidate', evidenceRefs: [], missingEvidence: true,
    });
    expect(() => runAtlasLocalCardAction(card('entity'), 'custom')).toThrow(/not an executable action/);
    expect(() => runAtlasLocalCardAction(card('entity'), 'unknown')).toThrow(/Unknown Atlas card action/);
  });

  it('exports deterministic escaped sourced text with provenance and missing-data labels', () => {
    const input = card('source', ['source-a']);
    const result = runAtlasLocalCardAction(input, 'export');
    expect(result.type).toBe('export');
    if (result.type !== 'export') throw new Error('Expected local export');
    expect(result.filename).toBe('atlas-sourced-summary.md');
    expect(result.content).toContain('&lt;Untrusted card&gt;');
    expect(result.content).toContain('&lt;script&gt; inert \\#\\# injected heading');
    expect(result.content).not.toContain('\n## injected heading');
    expect(result.content).toContain('## Provenance\n- source-a');
    expect(result.content).toContain('- Unknown availability');
    expect(runAtlasLocalCardAction(input, 'export')).toEqual(result);
  });
});

const binding = {
  cardId: 'card-1', workspaceRevision: 'workspace-4', branchRevision: null,
  evidenceRefs: ['source-a'],
};
const draft = (): AtlasActionDraftV1 => ({
  schema_version: 'atlas-action-draft.v1',
  proposal_id: 'draft-1',
  card_reference: { card_id: 'card-1', workspace_revision: 'workspace-4', branch_revision: null },
  intent_text: 'Ask the user what information they want.',
  capability: {
    capability_id: null, schema_version: null, resolution: 'unverified',
    reason: 'No trusted external capability catalog is available.',
  },
  proposed_arguments: [{ field: 'requested_information', value: 'hours',
    source: 'user_input', uncertain: false }],
  evidence_refs: ['source-a'],
  missing_fields: ['user confirmation'],
  warnings: ['Untrusted suggestion only.'],
  external_execution: {
    status: 'unavailable',
    reason: 'External execution unavailable — authorization release gates pending',
  },
});

describe('strict saved action draft parsing', () => {
  it('accepts a schema-shaped untrusted draft bound to the exact card and evidence', () => {
    const value = draft();
    const parsed = parseAtlasActionDraft(value, binding);
    expect(parsed).toEqual(value);
    expect(parsed).not.toBe(value);
  });

  it.each([
    ['unknown top-level field', (d: Record<string, unknown>) => { d.tool_name = 'invented'; }],
    ['executable authority', (d: Record<string, unknown>) => {
      d.external_execution = { status: 'approved', reason: 'ready' };
    }],
    ['unknown capability id', (d: Record<string, unknown>) => {
      d.capability = { capability_id: 'invented-tool', schema_version: 'v1',
        resolution: 'unverified', reason: 'untrusted' };
    }],
  ])('rejects %s', (_, mutate) => {
    const value = draft() as unknown as Record<string, unknown>;
    mutate(value);
    expect(() => parseAtlasActionDraft(value, binding)).toThrow(/Invalid Atlas action draft|capability/);
  });

  it('rejects stale card/revision bindings and evidence not attached to the card', () => {
    const value = draft();
    expect(() => parseAtlasActionDraft(value, { ...binding, workspaceRevision: 'workspace-5' }))
      .toThrow(/revision/);
    expect(() => parseAtlasActionDraft({ ...value, evidence_refs: ['unrelated'] }, binding))
      .toThrow(/evidence/);
  });

  it.each([
    { field: 'destination', value: 'Clinic A' },
    { field: 'requested_information', value: 'https://arbitrary.example' },
    { field: 'command', value: 'do something' },
  ])('rejects arbitrary destination/executable argument %#', argument => {
    const value = { ...draft(), proposed_arguments: [{
      ...argument, source: 'user_input' as const, uncertain: false,
    }] };
    expect(() => parseAtlasActionDraft(value, binding)).toThrow(/destination or executable field/);
  });

  it('rejects a destination embedded in draft text and schema-invalid URL evidence', () => {
    expect(() => parseAtlasActionDraft({ ...draft(), intent_text: 'Use https://arbitrary.example' }, binding))
      .toThrow(/destination or executable field/);
    expect(() => parseAtlasActionDraft({ ...draft(), evidence_refs: ['https://arbitrary.example'] }, binding))
      .toThrow(/Invalid Atlas action draft/);
    const unsafeRef = 'ftp://arbitrary.example';
    expect(() => parseAtlasActionDraft({ ...draft(), evidence_refs: [unsafeRef] }, {
      ...binding, evidenceRefs: [unsafeRef],
    })).toThrow(/destination-like evidence/);
  });

  it.each([
    ['capability reason', (value: AtlasActionDraftV1) => {
      value.capability.reason = 'Send to https://arbitrary.example';
    }],
    ['missing field', (value: AtlasActionDraftV1) => {
      value.missing_fields = ['Provide https://arbitrary.example'];
    }],
    ['warning', (value: AtlasActionDraftV1) => {
      value.warnings = ['Use https://arbitrary.example'];
    }],
  ])('rejects destination text in %s', (_, mutate) => {
    const value = draft();
    mutate(value);
    expect(() => parseAtlasActionDraft(value, binding)).toThrow(/destination or executable field/);
  });
});
