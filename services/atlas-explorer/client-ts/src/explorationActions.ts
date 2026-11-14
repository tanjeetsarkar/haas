import Ajv from 'ajv';

export type AtlasActionCardKind = 'entity' | 'category' | 'facet' | 'source' | 'candidate' | 'unavailable';

export interface AtlasSourcedFact {
  label: string;
  value: string;
  evidenceRefs: readonly string[];
}

/** Minimal, explicit card context. This module does not fetch or enrich it. */
export interface AtlasActionCard {
  cardId: string;
  kind: AtlasActionCardKind;
  title: string;
  workspaceRevision: string | null;
  branchRevision: string | null;
  evidenceRefs: readonly string[];
  sourcedFacts?: readonly AtlasSourcedFact[];
  missingFields?: readonly string[];
}

export type AtlasCardAction =
  | { id: 'inspect'; label: 'Inspect sources' | 'Inspect missing evidence'; kind: 'local';
      operation: 'inspect_sources' | 'inspect_missing_evidence' }
  | { id: 'export'; label: 'Export sourced summary'; kind: 'local'; operation: 'export_sourced_summary' }
  | { id: 'custom'; label: 'Custom action'; kind: 'custom'; operation: 'draft_intent';
      externalExecution: 'unavailable' };

export interface AtlasActionDraftV1 {
  schema_version: 'atlas-action-draft.v1';
  proposal_id: string;
  card_reference: { card_id: string; workspace_revision: string | null; branch_revision: string | null };
  intent_text: string;
  capability: {
    capability_id: string | null;
    schema_version: string | null;
    resolution: 'unverified';
    reason: string;
  };
  proposed_arguments: Array<{
    field: string; value: string; source: 'user_input' | 'public_card'; uncertain: boolean;
  }>;
  evidence_refs: string[];
  missing_fields: string[];
  warnings: string[];
  external_execution: {
    status: 'unavailable';
    reason: 'External execution unavailable — authorization release gates pending';
  };
}

const draftSchema = {
  type: 'object', additionalProperties: false,
  required: ['schema_version', 'proposal_id', 'card_reference', 'intent_text', 'capability',
    'proposed_arguments', 'evidence_refs', 'missing_fields', 'warnings', 'external_execution'],
  properties: {
    schema_version: { type: 'string', const: 'atlas-action-draft.v1' },
    proposal_id: { type: 'string', minLength: 1, maxLength: 128 },
    card_reference: {
      type: 'object', additionalProperties: false,
      required: ['card_id', 'workspace_revision', 'branch_revision'],
      properties: {
        card_id: { type: 'string', minLength: 1, maxLength: 128 },
        workspace_revision: { type: ['string', 'null'], maxLength: 128 },
        branch_revision: { type: ['string', 'null'], maxLength: 128 },
      },
    },
    intent_text: { type: 'string', minLength: 1, maxLength: 2000 },
    capability: {
      type: 'object', additionalProperties: false,
      required: ['capability_id', 'schema_version', 'resolution', 'reason'],
      properties: {
        capability_id: { type: ['string', 'null'], minLength: 1, maxLength: 128 },
        schema_version: { type: ['string', 'null'], minLength: 1, maxLength: 64 },
        resolution: { type: 'string', const: 'unverified' },
        reason: { type: 'string', minLength: 1, maxLength: 500 },
      },
    },
    proposed_arguments: {
      type: 'array', maxItems: 32, items: {
        type: 'object', additionalProperties: false,
        required: ['field', 'value', 'source', 'uncertain'],
        properties: {
          field: { type: 'string', minLength: 1, maxLength: 128 },
          value: { type: 'string', maxLength: 1000 },
          source: { type: 'string', enum: ['user_input', 'public_card'] },
          uncertain: { type: 'boolean' },
        },
      },
    },
    evidence_refs: {
      type: 'array', maxItems: 32, uniqueItems: true,
      items: { type: 'string', minLength: 1, maxLength: 256, pattern: '^(?!https?://).+$' },
    },
    missing_fields: { type: 'array', maxItems: 32, uniqueItems: true,
      items: { type: 'string', minLength: 1, maxLength: 256 } },
    warnings: { type: 'array', maxItems: 32,
      items: { type: 'string', minLength: 1, maxLength: 500 } },
    external_execution: {
      type: 'object', additionalProperties: false, required: ['status', 'reason'],
      properties: {
        status: { type: 'string', const: 'unavailable' },
        reason: { type: 'string',
          const: 'External execution unavailable — authorization release gates pending' },
      },
    },
  },
} as const;
const validateDraft = new Ajv({ allErrors: true }).compile<AtlasActionDraftV1>(draftSchema);

const unsafeArgumentField = /(?:destination|endpoint|url|uri|host|tool|command|code|script|approval|authority)/iu;
const destinationValue = /(?:\b[a-z][a-z0-9+.-]*:\/\/|javascript:|www\.)/iu;

function safeEvidenceRefs(card: AtlasActionCard): string[] {
  return [...new Set(card.evidenceRefs.filter(ref => ref.length > 0 && ref.length <= 256 &&
    ref.trim() === ref && !/[\u0000-\u001f\u007f]/u.test(ref) && !destinationValue.test(ref)))];
}

function groundedFacts(card: AtlasActionCard): AtlasSourcedFact[] {
  const refs = new Set(safeEvidenceRefs(card));
  return (card.sourcedFacts ?? []).filter(fact =>
    fact.label.length > 0 && fact.value.length > 0 && fact.evidenceRefs.length > 0 &&
    fact.evidenceRefs.every(ref => refs.has(ref)));
}

/** Static, deterministic affordances. There is deliberately no external/tool action. */
export function getAtlasCardActions(card: AtlasActionCard): AtlasCardAction[] {
  const evidenceRefs = safeEvidenceRefs(card);
  const actions: AtlasCardAction[] = [{
    id: 'inspect',
    label: evidenceRefs.length ? 'Inspect sources' : 'Inspect missing evidence',
    kind: 'local',
    operation: evidenceRefs.length ? 'inspect_sources' : 'inspect_missing_evidence',
  }];
  if (groundedFacts(card).length) {
    actions.push({ id: 'export', label: 'Export sourced summary', kind: 'local',
      operation: 'export_sourced_summary' });
  }
  actions.push({ id: 'custom', label: 'Custom action', kind: 'custom', operation: 'draft_intent',
    externalExecution: 'unavailable' });
  return actions;
}

export type AtlasLocalActionResult =
  | { type: 'inspection'; cardId: string; evidenceRefs: string[]; missingEvidence: boolean }
  | { type: 'export'; filename: string; content: string };

function escapeText(value: string): string {
  return value.replace(/[\u0000-\u001f\u007f]/gu, ' ').replace(/&/gu, '&amp;')
    .replace(/</gu, '&lt;').replace(/>/gu, '&gt;').replace(/"/gu, '&quot;')
    .replace(/'/gu, '&#39;').replace(/([\\`*_{}\[\]()#+.!|>])/gu, '\\$1');
}

/** Pure local action handler; callers invoke it only after an explicit user click. */
export function runAtlasLocalCardAction(card: AtlasActionCard, actionId: string): AtlasLocalActionResult {
  const action = getAtlasCardActions(card).find(candidate => candidate.id === actionId);
  if (!action) throw new Error('Unknown Atlas card action');
  if (action.kind !== 'local') throw new Error('Custom action is a draft affordance, not an executable action');
  if (action.operation !== 'export_sourced_summary') {
    return { type: 'inspection', cardId: card.cardId,
      evidenceRefs: safeEvidenceRefs(card), missingEvidence: action.operation === 'inspect_missing_evidence' };
  }
  const facts = groundedFacts(card);
  const provenance = [...new Set(facts.flatMap(fact => fact.evidenceRefs))].sort();
  const lines = [
    `# ${escapeText(card.title)}`,
    '',
    ...facts.map(fact => `- **${escapeText(fact.label)}:** ${escapeText(fact.value)} _(sources: ${
      fact.evidenceRefs.map(escapeText).join(', ')
    })_`),
    '',
    '## Provenance',
    ...provenance.map(ref => `- ${escapeText(ref)}`),
    '',
    '## Missing data',
    ...(card.missingFields?.length
      ? card.missingFields.map(field => `- ${escapeText(field)}`)
      : ['- No missing-field metadata was supplied.']),
  ];
  return { type: 'export', filename: 'atlas-sourced-summary.md', content: lines.join('\n') };
}

export interface AtlasDraftBinding {
  cardId: string;
  workspaceRevision: string | null;
  branchRevision: string | null;
  evidenceRefs: readonly string[];
}

/** Strictly validates the skill draft and binds it to the currently selected card snapshot. */
export function parseAtlasActionDraft(value: unknown, binding: AtlasDraftBinding): AtlasActionDraftV1 {
  if (!validateDraft(value)) throw new Error(`Invalid Atlas action draft: ${new Ajv().errorsText(validateDraft.errors)}`);
  const draft = value;
  if (draft.capability.capability_id !== null || draft.capability.schema_version !== null) {
    throw new Error('Atlas action draft cannot claim an unregistered capability or schema');
  }
  if (draft.card_reference.card_id !== binding.cardId ||
      draft.card_reference.workspace_revision !== binding.workspaceRevision ||
      draft.card_reference.branch_revision !== binding.branchRevision) {
    throw new Error('Atlas action draft does not match the selected card revision');
  }
  const allowedEvidence = new Set(binding.evidenceRefs);
  if (draft.evidence_refs.some(ref => !allowedEvidence.has(ref) || destinationValue.test(ref))) {
    throw new Error('Atlas action draft contains untrusted or destination-like evidence');
  }
  const untrustedText = [
    draft.proposal_id, draft.intent_text, draft.capability.reason,
    ...draft.proposed_arguments.flatMap(arg => [arg.field, arg.value]),
    ...draft.evidence_refs, ...draft.missing_fields, ...draft.warnings,
  ];
  if (draft.proposed_arguments.some(arg => unsafeArgumentField.test(arg.field)) ||
      untrustedText.some(text => destinationValue.test(text))) {
    throw new Error('Atlas action draft contains an arbitrary destination or executable field');
  }
  return structuredClone(draft);
}
