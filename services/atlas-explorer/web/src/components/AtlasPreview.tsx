import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import {
  activateExplorationPath,
  getExplorationBreadcrumbs,
  selectExplorationTarget,
  type AtlasExplorationSnapshot,
  type AtlasOccurrence,
  type AtlasTarget,
} from '@haas/atlas-explorer-client';
import { FIXTURE_FACET_RELATIONSHIPS, SAMPLE_SNAPSHOTS, type SampleKey } from '../previewFixtures';
import { PreviewConnections } from './PreviewConnections';
import { AtlasGraphColumn } from './AtlasGraphColumn';
import { AtlasInspectorFrame, EvidenceLabel, RelationshipEvidence, SourceList, SourceDetails, sourceTitles } from './AtlasInspector';
import './AtlasPreview.css';

type Props = { onExit: () => void };
type InspectorTarget = AtlasTarget | null;

const SAMPLE_QUERY: Record<SampleKey, string> = { computers: 'computers', people: 'Avery Rowan' };

function getTargetTitle(snapshot: AtlasExplorationSnapshot, target: AtlasTarget): string {
  if (target.kind === 'source') {
    return snapshot.sources.find(source => source.source_id === target.source_id)?.title ?? 'Source';
  }
  const entity = snapshot.entities.find(row => row.entity_id === target.entity_id);
  if (target.kind === 'entity') return entity?.title ?? 'Entity';
  return entity?.facets.find(facet => facet.key === target.facet_key)?.label ?? 'Facet';
}

function getTargetType(target: AtlasTarget): string {
  return target.kind === 'entity' ? 'Entity' : target.kind === 'facet' ? 'Facet' : 'Source';
}

function targetKey(target: AtlasTarget): string {
  if (target.kind === 'source') return `source:${target.source_id}`;
  if (target.kind === 'entity') return `entity:${target.entity_id}`;
  return `facet:${target.entity_id}:${target.facet_key}`;
}

function findActivePath(snapshot: AtlasExplorationSnapshot) {
  return snapshot.paths.find(path => path.path_id === snapshot.active_path_id) ?? null;
}

function facetFor(snapshot: AtlasExplorationSnapshot, target: AtlasTarget) {
  if (target.kind !== 'facet') return undefined;
  return snapshot.entities.find(entity => entity.entity_id === target.entity_id)
    ?.facets.find(facet => facet.key === target.facet_key);
}

function actionMatchesTarget(actionTarget: AtlasTarget, selectedTarget: AtlasTarget): boolean {
  if (actionTarget.kind === 'source' || selectedTarget.kind === 'source') {
    return actionTarget.kind === 'source' && selectedTarget.kind === 'source' &&
      actionTarget.source_id === selectedTarget.source_id;
  }
  if (actionTarget.entity_id !== selectedTarget.entity_id) return false;
  return actionTarget.kind === 'entity' ||
    (selectedTarget.kind === 'facet' && actionTarget.facet_key === selectedTarget.facet_key);
}

export function AtlasPreview({ onExit }: Props) {
  const [snapshot, setSnapshot] = useState<AtlasExplorationSnapshot>(SAMPLE_SNAPSHOTS.computers);
  const [templateKey, setTemplateKey] = useState<SampleKey>('computers');
  const [sampleChoice, setSampleChoice] = useState<SampleKey>('computers');
  const [query, setQuery] = useState('computers');
  const [queryUnavailable, setQueryUnavailable] = useState(false);
  const [message, setMessage] = useState('Showing a local sample. Nothing is being researched or saved.');
  const [inspectorTarget, setInspectorTarget] = useState<InspectorTarget>(null);
  const [error, setError] = useState<string | null>(null);
  const [mobileColumn, setMobileColumn] = useState(0);
  const railRef = useRef<HTMLDivElement>(null);
  const pendingParentFocus = useRef<number | null>(null);

  const activePath = findActivePath(snapshot);
  const exploredEntityIds = new Set(snapshot.paths.flatMap(path => path.selected_occurrence_ids)
    .map(id => snapshot.occurrences.find(occurrence => occurrence.occurrence_id === id)?.target)
    .filter(target => target?.kind === 'entity')
    .map(target => target.entity_id));
  const startingPath = snapshot.paths.find(path =>
    path.parent_path_id === null && path.selected_occurrence_ids.length === 0);
  const selectedOccurrences = getExplorationBreadcrumbs(snapshot);
  const columns = useMemo(() => {
    // Fixture occurrences are templates keyed by canonical target identity, so the same
    // entity/facet discloses the same children regardless of the path that reached it.
    const template = SAMPLE_SNAPSHOTS[templateKey].occurrences;
    const templateTargets = new Map(template.map(row => [row.occurrence_id, targetKey(row.target)]));
    const parents: (AtlasOccurrence | null)[] = [null, ...selectedOccurrences];
    return parents.map(parent => {
      const parentKey = parent ? targetKey(parent.target) : null;
      const candidates = queryUnavailable ? [] : template.filter(row =>
        (row.parent_occurrence_id === null ? null : templateTargets.get(row.parent_occurrence_id)) === parentKey);
      const uniqueTargets = new Map<string, AtlasOccurrence>();
      candidates.forEach(row => {
        const key = targetKey(row.target);
        if (!uniqueTargets.has(key)) {
          uniqueTargets.set(key, {
            occurrence_id: `${parent?.occurrence_id ?? 'root'}:${key}`,
            path_id: parent?.path_id ?? startingPath?.path_id ?? '',
            parent_occurrence_id: parent?.occurrence_id ?? null,
            target: row.target,
          });
        }
      });
      return {
        title: parent ? getTargetTitle(snapshot, parent.target) : 'Starting points',
        rows: [...uniqueTargets.values()],
      };
    });
  }, [queryUnavailable, selectedOccurrences, snapshot, startingPath?.path_id, templateKey]);

  const activeColumn = Math.min(mobileColumn, columns.length - 1);
  const selectedTarget = inspectorTarget ??
    (selectedOccurrences.length ? selectedOccurrences[selectedOccurrences.length - 1].target : null);
  const selectedEntityId = selectedTarget?.kind === 'source'
    ? null
    : selectedTarget?.entity_id ?? (snapshot.interpretation.kind === 'resolved' ? snapshot.interpretation.subject_entity_id : null);
  const currentFacet = selectedTarget ? facetFor(snapshot, selectedTarget) : undefined;
  const visibleClaims = snapshot.claims.filter(claim =>
    claim.subject_entity_id === selectedEntityId &&
    (selectedTarget?.kind !== 'facet' || claim.facet_key === selectedTarget.facet_key || claim.facet_key === undefined));
  const connections = snapshot.relationships.filter(relationship =>
    selectedEntityId !== null &&
    (relationship.from_entity_id === selectedEntityId || relationship.to_entity_id === selectedEntityId) &&
    (selectedTarget?.kind !== 'facet' ||
      (FIXTURE_FACET_RELATIONSHIPS[selectedTarget.entity_id]?.[selectedTarget.facet_key] ?? [])
        .includes(relationship.relationship_id)));

  useEffect(() => {
    const rail = railRef.current;
    if (rail && window.innerWidth > 600) {
      rail.scrollLeft = rail.scrollWidth;
    }
  }, [columns.length, snapshot.active_path_id]);

  useEffect(() => {
    const index = pendingParentFocus.current;
    if (index === null) return;
    pendingParentFocus.current = null;
    const column = railRef.current?.querySelectorAll<HTMLElement>('.atlas-preview__column')[index];
    column?.querySelector<HTMLButtonElement>('[data-atlas-row][aria-current="step"]')?.focus();
  });

  function loadSample(key: SampleKey) {
    try {
      const loaded = SAMPLE_SNAPSHOTS[key];
      setSnapshot(loaded);
      setTemplateKey(key);
      setQueryUnavailable(false);
      setQuery(SAMPLE_QUERY[key]);
      setSampleChoice(key);
      setMessage(`Loaded the “${SAMPLE_QUERY[key]}” fixture. All details are synthetic and local.`);
      setInspectorTarget(null);
      setMobileColumn(0);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The sample could not be loaded.');
    }
  }

  function submitQuery(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = query.trim().toLocaleLowerCase();
    const key = (Object.keys(SAMPLE_QUERY) as SampleKey[]).find(sample =>
      SAMPLE_QUERY[sample].toLocaleLowerCase() === normalized);
    if (key) {
      loadSample(key);
      return;
    }
    setMessage('Preview contains samples only; live research is unavailable. Choose a sample query to explore fixture content.');
    setSnapshot(SAMPLE_SNAPSHOTS.computers);
    setTemplateKey('computers');
    setQueryUnavailable(true);
    setInspectorTarget(null);
    setMobileColumn(0);
  }

  function openOccurrence(occurrence: AtlasOccurrence, columnIndex: number) {
    try {
      const next = selectExplorationTarget(snapshot, occurrence.target, occurrence.parent_occurrence_id);
      const selectedPath = next.paths.find(path => path.path_id === next.active_path_id);
      const selectedOccurrenceId = selectedPath?.selected_occurrence_ids[selectedPath.selected_occurrence_ids.length - 1];
      const selectedOccurrence = next.occurrences.find(row => row.occurrence_id === selectedOccurrenceId);
      setSnapshot(next);
      const activeTarget = selectedOccurrence?.target ?? occurrence.target;
      setInspectorTarget(activeTarget);
      setMobileColumn(selectedPath?.selected_occurrence_ids.length ?? columnIndex + 1);
      setMessage(`${getTargetType(activeTarget)} selected: ${getTargetTitle(snapshot, activeTarget)}.`);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'This sample branch could not be opened.');
    }
  }

  function activatePath(pathId: string, mobileIndex?: number) {
    try {
      const next = activateExplorationPath(snapshot, pathId);
      const activatedPath = next.paths.find(path => path.path_id === pathId);
      setSnapshot(next);
      setInspectorTarget(null);
      setMobileColumn(mobileIndex ?? activatedPath?.selected_occurrence_ids.length ?? 0);
      setMessage('Visited path activated. Changes remain in memory only.');
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'This in-memory path could not be activated.');
    }
  }

  function navigateConnection(entityId: string) {
    const parent = selectedOccurrences[selectedOccurrences.length - 1];
    openOccurrence({
      occurrence_id: 'connection-preview-selection',
      path_id: activePath?.path_id ?? '',
      parent_occurrence_id: parent?.occurrence_id ?? null,
      target: { kind: 'entity', entity_id: entityId },
    }, selectedOccurrences.length);
  }

  function goBack() {
    if (activePath?.parent_path_id) {
      activatePath(activePath.parent_path_id, Math.max(0, columns.length - 2));
    } else if (activePath?.selected_occurrence_ids.length === 1 && startingPath) {
      activatePath(startingPath.path_id, 0);
    } else {
      setMobileColumn(Math.max(0, mobileColumn - 1));
      setInspectorTarget(null);
    }
  }

  function handleColumnKey(event: KeyboardEvent<HTMLElement>, rows: AtlasOccurrence[], index: number) {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-atlas-row]'));
    const focused = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      buttons[(focused + delta + buttons.length) % buttons.length]?.focus();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      const parent = selectedOccurrences[index - 1];
      if (parent) {
        pendingParentFocus.current = index - 1;
        activatePath(parent.path_id, index - 1);
      }
    } else if (event.key === 'ArrowRight' && focused >= 0) {
      event.preventDefault();
      const occurrence = rows[focused];
      if (occurrence) openOccurrence(occurrence, index);
    }
  }

  return (
    <main className="atlas-preview" aria-label="Atlas fixture preview">
      <header className="atlas-preview__header">
        <div className="atlas-preview__brand">
          <div className="atlas-preview__eyebrow">ATLAS · INTERACTIVE PREVIEW</div>
          <h1>Explore a subject, one useful step at a time</h1>
          <p>Follow facets, inspect evidence, and keep sibling paths in view.</p>
        </div>
        <button className="atlas-preview__exit" type="button" onClick={onExit}>← Back to Atlas</button>
      </header>

      <div className="atlas-preview__notice" role="note">
        <span aria-hidden="true">✦</span>
        <div><strong>Fixture-only preview · local and unsaved</strong><br />
          Fictional sample content; no live search, network fetch, or persistence. Sources use reserved example.test addresses and are unverified.</div>
      </div>

      <form className="atlas-preview__search" onSubmit={submitQuery} aria-label="Choose an Atlas sample">
        <label htmlFor="atlas-preview-query">Try a sample or enter a query</label>
        <div className="atlas-preview__search-controls">
          <input id="atlas-preview-query" value={query} onChange={event => setQuery(event.target.value)} />
          <select aria-label="Sample query choices" value={sampleChoice} onChange={event => setSampleChoice(event.target.value as SampleKey)}>
            <option value="computers">computers</option>
            <option value="people">Avery Rowan (fictional)</option>
          </select>
          <button type="button" onClick={() => loadSample(sampleChoice)}>Load sample</button>
          <button className="atlas-preview__search-button" type="submit">Preview query</button>
        </div>
      </form>

      {message && <div className="atlas-preview__status" role="status">{message}</div>}
      {error && <div className="atlas-preview__error" role="alert">{error}</div>}

      <nav className="atlas-preview__breadcrumbs" aria-label="Selected ancestry">
        <span>Path</span>
        <button type="button" onClick={() => startingPath && activatePath(startingPath.path_id, 0)}>Start</button>
        {selectedOccurrences.map((occurrence, index) => (
          <span className="atlas-preview__crumb" key={occurrence.occurrence_id}>
            <span aria-hidden="true">/</span>
            <button type="button" onClick={() => activatePath(occurrence.path_id, index + 1)}>
              {getTargetTitle(snapshot, occurrence.target)}
            </button>
          </span>
        ))}
      </nav>

      <div className="atlas-preview__workspace">
        <section className="atlas-preview__navigation" aria-label="Exploration columns">
          <div className="atlas-preview__rail" ref={railRef}>
            {columns.map((column, index) => (
              <AtlasGraphColumn key={index} snapshot={snapshot} title={column.title} rows={column.rows}
                index={index} active={index === activeColumn} selected={selectedOccurrences[index]?.target}
                explored={exploredEntityIds} sample onSelect={openOccurrence} onKeyDown={handleColumnKey}
                emptyMessage={queryUnavailable ? 'No matching fixture. Live research is unavailable.' : 'No further sample steps here.'} />
            ))}
          </div>
          <div className="atlas-preview__mobile-controls">
            <button type="button" onClick={goBack} disabled={mobileColumn === 0 && !activePath?.parent_path_id}>← Back</button>
            <span>Column {activeColumn + 1} of {columns.length}</span>
          </div>
          <nav className="atlas-preview__visited" aria-label="Visited paths">
            <span>Visited paths</span>
            {snapshot.paths.filter(path => path.selected_occurrence_ids.length > 0).map(path => {
              const label = path.selected_occurrence_ids.map(id => {
                const occurrence = snapshot.occurrences.find(row => row.occurrence_id === id);
                return occurrence ? getTargetTitle(snapshot, occurrence.target) : 'Starting point';
              }).join(' › ');
              return (
                <button type="button" key={path.path_id} title={label} aria-label={label}
                  aria-current={path.path_id === snapshot.active_path_id ? 'page' : undefined}
                  onClick={() => activatePath(path.path_id)}>
                  {label}
                </button>
              );
            })}
            {snapshot.paths.filter(path => path.selected_occurrence_ids.length > 0).length === 0 &&
              <span className="atlas-preview__visited-empty">No branches visited yet</span>}
          </nav>
        </section>

        <AtlasInspectorFrame sample>
          {selectedTarget ? (
            <>
              <div className="atlas-preview__detail-title">
                <span>{getTargetType(selectedTarget)}</span>
                <h2>{getTargetTitle(snapshot, selectedTarget)}</h2>
                {currentFacet && <p>{currentFacet.scope}</p>}
              </div>
              {currentFacet?.status === 'research_needed' &&
                <div className="atlas-preview__facet-note"><strong>Research needed</strong><br />No research is running in this fixture preview. This sample does not contain details for this facet.</div>}
              {currentFacet?.status === 'unavailable' &&
                <div className="atlas-preview__facet-note"><strong>Unavailable in sample</strong><br />{currentFacet.reason} No action is offered to start research.</div>}
              {selectedTarget.kind === 'source' ? (
                <SourceDetails snapshot={snapshot} sourceId={selectedTarget.source_id} />
              ) : (
                <>
                  <section className="atlas-preview__detail-section" aria-label="Claims and evidence">
                    <h3>Claims & evidence</h3>
                    {visibleClaims.length === 0 ? <p className="atlas-preview__muted">No fixture claims are attached here.</p> : visibleClaims.map(claim => (
                      <article className="atlas-preview__claim" key={claim.claim_id}>
                        <p>{claim.text}</p>
                        <EvidenceLabel status={claim.evidence.status} />
                        {claim.evidence.status === 'unsupported' ? <p className="atlas-preview__muted">{claim.evidence.reason}</p> : null}
                        {claim.evidence.status === 'conflicting' && <div className="atlas-preview__conflict">
                          <span>Supports: {sourceTitles(snapshot, claim.evidence.supporting_source_ids).join(', ')}</span>
                          <span>Differs: {sourceTitles(snapshot, claim.evidence.opposing_source_ids).join(', ')}</span>
                        </div>}
                        {claim.evidence.status === 'sourced' && <p className="atlas-preview__muted">Evidence: {sourceTitles(snapshot, claim.evidence.source_ids).join(', ')}</p>}
                      </article>
                    ))}
                  </section>
                  {selectedEntityId && <SourceList snapshot={snapshot} entityId={selectedEntityId}
                    facetKey={selectedTarget.kind === 'facet' ? selectedTarget.facet_key : undefined}
                    relationshipEvidence={connections.map(connection => connection.evidence)} />}
                  <section className="atlas-preview__detail-section" aria-label="Related connections">
                    <h3>Related connections</h3>
                    {selectedEntityId && <PreviewConnections snapshot={snapshot} entityId={selectedEntityId}
                      relationships={connections} onNavigate={navigateConnection} />}
                    {connections.length === 0 ? <p className="atlas-preview__muted">No relationships in this sample.</p> : connections.map(connection => {
                      const otherId = connection.from_entity_id === selectedEntityId ? connection.to_entity_id : connection.from_entity_id;
                      const other = snapshot.entities.find(entity => entity.entity_id === otherId);
                      return <div className="atlas-preview__connection" key={connection.relationship_id}>
                        <span>{connection.to_entity_id === selectedEntityId ? 'Incoming: ' : 'Outgoing: '}{connection.label}</span>
                        <strong>{other?.title ?? 'Related subject'}</strong>
                        <small>{connection.verification === 'confirmed' ? 'Sourced relationship · synthetic' : 'Suggested relationship · synthetic'}</small>
                        <RelationshipEvidence snapshot={snapshot} evidence={connection.evidence} />
                      </div>;
                    })}
                  </section>
                  <section className="atlas-preview__detail-section">
                    <h3>Actions</h3>
                    <p className="atlas-preview__muted">External actions are disabled in preview. Any real-world action would require explicit review and approval.</p>
                    {snapshot.actions.filter(action => actionMatchesTarget(action.target, selectedTarget)).map(action =>
                      <button type="button" className="atlas-preview__disabled-action" disabled key={action.action_id} title="Preview cannot execute external actions">
                        {action.label} · approval required
                      </button>)}
                  </section>
                </>
              )}
            </>
          ) : (
            <div className="atlas-preview__inspector-empty">
              <span aria-hidden="true">↖</span>
              <h2>Select a starting point</h2>
              <p>Choose an entity, facet, or source to open its next step and inspect the sample evidence here.</p>
            </div>
          )}
        </AtlasInspectorFrame>
      </div>

      <footer className="atlas-preview__footer">
        <span>Fixture data only · changes are not saved</span>
        <button type="button" onClick={onExit}>Return to the live Atlas workspace</button>
      </footer>
    </main>
  );
}
