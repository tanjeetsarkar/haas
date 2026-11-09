import { describe, expect, expectTypeOf, it } from 'vitest';
import type {
  AtlasEventHandler,
  AtlasLayerEnvelope,
  AtlasLayerNode,
  AtlasRunEvent,
  DrilldownRequest,
} from '../src';
import type { RunEvent } from '@haas/sdk';

const node = {
  node_id: 'hospital-1',
  canonical_key: 'hospital:mumbai:1',
  title: 'Example Hospital',
  component_type: 'entity_card',
  attributes: { beds: 120, accepting_patients: true, note: null },
  provenance: {
    trust_tier: 'verified_official',
    source_urls: ['https://example.test/hospital'],
    observed_at: '2026-11-09T00:00:00Z',
  },
} satisfies AtlasLayerNode;

const envelope = {
  schema_version: 'atlas-layer.v1',
  layer_depth: 1,
  parent_node_id: null,
  domain_category: 'healthcare',
  nodes: [node],
  available_drilldowns: [{
    dimension_key: 'specialties',
    label: 'Specialties',
    prompt_template: 'Explore specialties',
    recommended_skills: ['research'],
  }],
  available_actions: [{
    action_id: 'contact',
    label: 'Contact',
    tool_name: 'contact_provider',
    requires_approval: true,
  }],
} satisfies AtlasLayerEnvelope;

const drilldown = {
  threadId: 'thread-1',
  parentBranchVersion: 3,
  parentNodeId: node.node_id,
  dimensionKey: 'specialties',
  prompt: 'Explore specialties',
} satisfies DrilldownRequest;

const event = {
  sequence: 0,
  type: 'output_delta',
  data: { output_delta: { envelope, progress: 0.5 } },
} satisfies AtlasRunEvent;

const handler: AtlasEventHandler = (received) => {
  const sdkEvent: RunEvent = received;
  expect(sdkEvent.sequence).toBe(0);
};

if (false) {
  // @ts-expect-error Only the canonical layer schema is supported.
  const invalidSchema: AtlasLayerEnvelope = { ...envelope, schema_version: 'atlas-layer.v2' };
  // @ts-expect-error Component names are restricted to the declared union.
  const invalidComponent: AtlasLayerNode = { ...node, component_type: 'unknown_card' };
  const invalidTrustTier: AtlasLayerNode = {
    ...node,
    provenance: {
      ...node.provenance,
      // @ts-expect-error Trust tiers are restricted to the declared union.
      trust_tier: 'untrusted_guess',
    },
  };
  void [invalidSchema, invalidComponent, invalidTrustTier];
}

describe('Atlas domain type contracts', () => {
  it('exports all five types and preserves RunEvent assignability', () => {
    expectTypeOf<AtlasRunEvent>().toMatchTypeOf<RunEvent>();
    expectTypeOf<AtlasEventHandler>().toBeFunction();
    handler(event);
    expect(drilldown.dimensionKey).toBe('specialties');
    expect(envelope.nodes[0]?.provenance.trust_tier).toBe('verified_official');
  });
});
