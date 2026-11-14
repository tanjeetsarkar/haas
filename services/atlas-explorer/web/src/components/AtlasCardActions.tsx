import { useState } from 'react';
import type { AtlasActionDraftV1, AtlasExplorationSnapshot, AtlasTarget } from '@haas/atlas-explorer-client';
import { getAtlasCardActions, parseAtlasActionDraft, runAtlasLocalCardAction } from '@haas/atlas-explorer-client';
import { evidenceSources, targetClaims, targetKey, targetTitle } from '../graphProjection';
import './AtlasCardActions.css';

export function atlasActionCard(snapshot: AtlasExplorationSnapshot, target: AtlasTarget) {
  const entity = target.kind === 'source' ? undefined
    : snapshot.entities.find(item => item.entity_id === target.entity_id);
  const facet = target.kind === 'facet' ? entity?.facets.find(item => item.key === target.facet_key) : undefined;
  const source = target.kind === 'source' ? snapshot.sources.find(item => item.source_id === target.source_id) : undefined;
  const claims = targetClaims(snapshot, target);
  const knownSources = new Set(snapshot.sources.map(item => item.source_id));
  const sourcedFacts = claims.filter(claim => claim.evidence.status === 'sourced'
    && claim.evidence.source_ids.every(ref => knownSources.has(ref))).map(claim => ({
    label: claim.facet_key ?? 'Reported finding',
    value: claim.text,
    evidenceRefs: evidenceSources(claim.evidence),
  }));
  const evidenceRefs = target.kind === 'source' ? source ? [target.source_id] : []
    : [...new Set(claims.flatMap(claim => evidenceSources(claim.evidence)).filter(ref => knownSources.has(ref)))];
  if (source) sourcedFacts.push({ label: 'Source record', value: `${source.title} · ${source.publisher}`,
    evidenceRefs: [source.source_id] });
  const missingFields = [
    ...(entity?.identity_status === 'candidate' ? ['Identity resolution'] : []),
    ...(facet?.status === 'unavailable' ? [facet.reason] : []),
    ...(!sourcedFacts.length ? ['Source-backed details'] : []),
  ];
  const kind = source ? 'source' : entity?.identity_status === 'candidate' ? 'candidate'
    : facet?.status === 'unavailable' ? 'unavailable'
      : entity?.kind === 'category' ? 'category' : target.kind;
  return {
    cardId: targetKey(target),
    kind,
    title: targetTitle(snapshot, target),
    workspaceRevision: String(snapshot.revision),
    branchRevision: snapshot.active_path_id,
    evidenceRefs,
    sourcedFacts,
    missingFields,
  } as const;
}

export function AtlasCardActions({ snapshot, target, drafts, editingCardId, draftText, onEdit, onDraftText,
  onSaveDraft }: {
  snapshot: AtlasExplorationSnapshot;
  target: AtlasTarget;
  drafts: Record<string, unknown>;
  editingCardId: string | null;
  draftText: string;
  onEdit: (cardId: string, initialText: string) => void;
  onDraftText: (value: string) => void;
  onSaveDraft?: (cardId: string, draft: AtlasActionDraftV1) => Promise<void>;
}) {
  const card = atlasActionCard(snapshot, target);
  const binding = { cardId: card.cardId, workspaceRevision: card.workspaceRevision,
    branchRevision: card.branchRevision, evidenceRefs: card.evidenceRefs };
  let saved: AtlasActionDraftV1 | undefined;
  try {
    if (drafts[card.cardId]) saved = parseAtlasActionDraft(drafts[card.cardId], binding);
  } catch { /* Stale or malformed saved drafts are never presented as current. */ }
  const [message, setMessage] = useState('');
  const [inspection, setInspection] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  function localAction(id: string) {
    const result = runAtlasLocalCardAction(card, id);
    if (result.type === 'inspection') {
      setInspection(result.missingEvidence
        ? ['No source references are attached to this card. No network request was made.']
        : result.evidenceRefs);
      setMessage('Local inspection only; no request was made.');
      return;
    }
    const blob = new Blob([result.content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = result.filename;
    anchor.click();
    URL.revokeObjectURL(url);
    setMessage('Sourced summary downloaded locally.');
  }

  async function saveDraft() {
    const intent = draftText.trim();
    if (!intent) { setMessage('Enter the action you want drafted.'); return; }
    const raw: AtlasActionDraftV1 = {
      schema_version: 'atlas-action-draft.v1',
      proposal_id: `local-${Date.now()}`,
      card_reference: { card_id: card.cardId, workspace_revision: card.workspaceRevision,
        branch_revision: card.branchRevision },
      intent_text: intent,
      capability: { capability_id: null, schema_version: null, resolution: 'unverified',
        reason: 'No trusted capability catalog is connected to this local draft.' },
      proposed_arguments: [],
      evidence_refs: [...card.evidenceRefs],
      missing_fields: [...card.missingFields],
      warnings: ['Locally saved custom draft; not model-generated. No capability has been verified.'],
      external_execution: { status: 'unavailable',
        reason: 'External execution unavailable — authorization release gates pending' },
    };
    try {
      const draft = parseAtlasActionDraft(raw, binding);
      setBusy(true);
      if (onSaveDraft) {
        await onSaveDraft(card.cardId, draft);
        setMessage('Draft saved locally to this workspace. Not model-generated.');
      } else {
        setMessage('Not durably saved: this view has no workspace storage.');
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Draft was not saved.');
    } finally { setBusy(false); }
  }

  return <section className="atlas-card-actions" aria-label={`${card.title} actions`}
    onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}>
    <h4>Actions</h4>
    <div className="atlas-card-actions__buttons">
      {getAtlasCardActions(card).map(action => action.kind === 'custom'
        ? <button type="button" key={action.id} onClick={() => {
          setMessage('');
          onEdit(card.cardId, saved?.intent_text ?? '');
        }}>{action.label}</button>
        : <button type="button" key={action.id} onClick={() => localAction(action.id)}>{action.label}</button>)}
    </div>
    {editingCardId === card.cardId && <div className="atlas-card-actions__editor">
      <label>Describe the action to draft
        <textarea value={draftText} maxLength={2000} onChange={event => onDraftText(event.target.value)} />
      </label>
      <button type="button" disabled={busy || !draftText.trim()} onClick={() => void saveDraft()}>
        {busy ? 'Saving draft…' : 'Save custom draft'}
      </button>
      {saved && <p>Previously saved draft restored. Editing it requires another save.</p>}
    </div>}
    <p className="atlas-card-actions__hold">External execution unavailable — authorization release gates pending.</p>
    {inspection && <ul aria-label="Local inspection results">
      {inspection.map(ref => <li key={ref}>{ref}</li>)}
    </ul>}
    {message && <p role="status">{message}</p>}
  </section>;
}
