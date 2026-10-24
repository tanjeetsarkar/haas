/**
 * MillerColumn - Single column in the Miller Columns / Drill-Down Stacks layout
 * Renders a list of AtlasLayerNode items with selection and drill-down handling
 */

import React, { useMemo, CSSProperties } from 'react';
import { AtlasLayerNode, AtlasLayerEnvelope } from '@haas/sdk';
import { EntityCard } from './EntityCard';
import { ComparisonRow } from './ComparisonRow';
import { PricingTierCard } from './PricingTierCard';
import { SpecialistProfile } from './SpecialistProfile';
import { ChecklistCard } from './ChecklistCard';
import './MillerColumn.css';

interface ColumnState {
  layerDepth: number;
  envelope: AtlasLayerEnvelope;
  parentNodeId: string | null;
  threadId: string;
  branchVersion: number;
  runId: string;
  isLoading: boolean;
}

interface MillerColumnProps {
  column: ColumnState;
  index: number;
  onDrillDown: (
    parentColumn: ColumnState,
    node: AtlasLayerNode,
    dimensionKey: string,
    prompt: string
  ) => void;
  isLast: boolean;
}

export const MillerColumn: React.FC<MillerColumnProps> = ({
  column,
  index,
  onDrillDown,
  isLast,
}) => {
  const { envelope, isLoading, layerDepth } = column;

  const renderNode = useMemo(() => {
    switch (envelope.nodes[0]?.component_type) {
      case 'comparison_row':
        return ComparisonRow;
      case 'pricing_tier_card':
        return PricingTierCard;
      case 'specialist_profile':
        return SpecialistProfile;
      case 'checklist_card':
        return ChecklistCard;
      case 'entity_card':
      default:
        return EntityCard;
    }
  }, [envelope.nodes[0]?.component_type]);

  const NodeComponent = renderNode;

  const availableDrilldowns = envelope.available_drilldowns || [];
  const availableActions = envelope.available_actions || [];

  const columnStyle: CSSProperties = {
    '--column-index': index,
  } as CSSProperties;

  if (isLoading && envelope.nodes.length === 0) {
    return (
      <div className="miller-column loading" style={columnStyle}>
        <div className="miller-column-header">
          <h2>Layer {layerDepth}</h2>
          {column.parentNodeId && (
            <span className="miller-column-breadcrumb">
              ← Drill-down from {column.parentNodeId}
            </span>
          )}
        </div>
        <div className="miller-column-content">
          <div className="miller-column-skeleton">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="skeleton-item" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div 
      className={`miller-column ${isLast ? 'last' : ''}`}
      style={columnStyle}
      role="region"
      aria-label={`Layer ${layerDepth}: ${envelope.domain_category}`}
    >
      <div className="miller-column-header">
        <h2>
          Layer {layerDepth}
          <span className="layer-domain">({envelope.domain_category})</span>
        </h2>
        {column.parentNodeId && (
          <span className="miller-column-breadcrumb">
            ← {column.parentNodeId}
          </span>
        )}
        {envelope.nodes.length > 0 && (
          <span className="miller-column-count">
            {envelope.nodes.length} result{envelope.nodes.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      <div className="miller-column-content">
        {envelope.nodes.map((node: AtlasLayerNode) => (
          <NodeComponent
            key={node.node_id}
            node={node}
            onDrillDown={(dimensionKey, prompt) => 
              onDrillDown(column, node, dimensionKey, prompt)
            }
            availableDrilldowns={availableDrilldowns}
            availableActions={availableActions}
          />
        ))}

        {envelope.nodes.length === 0 && !isLoading && (
          <div className="miller-column-empty">
            <p>No results found for this layer</p>
          </div>
        )}

        {/* Drill-down chips at bottom of column */}
        {availableDrilldowns.length > 0 && (
          <div className="miller-column-drilldowns">
            <h3>Drill down further</h3>
            <div className="drilldown-chips">
              {availableDrilldowns.map((drilldown: { dimension_key: string; label: string; prompt_template: string }) => (
                <button
                  key={drilldown.dimension_key}
                  className="drilldown-chip"
                  onClick={() => {
                    const prompt = drilldown.prompt_template
                      .replace('{entity}', envelope.nodes[0]?.title || '');
                    onDrillDown(column, envelope.nodes[0], drilldown.dimension_key, prompt);
                  }}
                  disabled={envelope.nodes.length === 0}
                >
                  {drilldown.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Action chips */}
        {availableActions.length > 0 && (
          <div className="miller-column-actions">
            <h3>Available actions</h3>
            <div className="action-chips">
              {availableActions.map((action: { action_id: string; label: string; tool_name: string; requires_approval: boolean }) => (
                <button
                  key={action.action_id}
                  className={`action-chip ${action.requires_approval ? 'requires-approval' : ''}`}
                  disabled={envelope.nodes.length === 0}
                >
                  {action.label}
                  {action.requires_approval && (
                    <span className="approval-badge" title="Requires human approval">🔒</span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Resize handle for non-last columns */}
      {!isLast && (
        <div className="miller-column-resizer" aria-label="Resize column" />
      )}
    </div>
  );
};