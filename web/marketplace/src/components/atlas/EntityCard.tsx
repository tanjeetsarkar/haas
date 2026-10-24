/**
 * EntityCard - Layer 1 component for root entities (hospitals, universities, products)
 */

import React from 'react';
import { AtlasLayerNode } from '@haas/sdk';
import './EntityCard.css';

interface EntityCardProps {
  node: AtlasLayerNode;
  onDrillDown: (dimensionKey: string, prompt: string) => void;
  availableDrilldowns: Array<{
    dimension_key: string;
    label: string;
    prompt_template: string;
    recommended_skills: string[];
  }>;
  availableActions: Array<{
    action_id: string;
    label: string;
    tool_name: string;
    requires_approval: boolean;
  }>;
}

export const EntityCard: React.FC<EntityCardProps> = ({
  node,
  onDrillDown,
  availableDrilldowns,
  availableActions,
}) => {
  const trustTierColors: Record<string, string> = {
    verified_official: '#10b981',
    extracted_document: '#3b82f6',
    aggregated_directory: '#8b5cf6',
    estimated_range: '#f59e0b',
    unverified: '#ef4444',
  };

  const trustTierLabels: Record<string, string> = {
    verified_official: 'Verified Official',
    extracted_document: 'Extracted Document',
    aggregated_directory: 'Aggregated Directory',
    estimated_range: 'Estimated Range',
    unverified: 'Unverified',
  };

  const formatAttributes = (attrs: Record<string, string | number | boolean | null>) => {
    return Object.entries(attrs)
      .filter(([_, v]) => v !== null && v !== undefined)
      .map(([k, v]) => (
        <div key={k} className="entity-attribute">
          <span className="attr-label">{k.replace(/_/g, ' ')}:</span>
          <span className="attr-value">{String(v)}</span>
        </div>
      ));
  };

  return (
    <article className="entity-card" data-node-id={node.node_id}>
      <div className="entity-card-header">
        <h3 className="entity-title">{node.title}</h3>
        {node.subtitle && (
          <p className="entity-subtitle">{node.subtitle}</p>
        )}
      </div>

      <div className="entity-card-body">
        <div className="entity-attributes">
          {formatAttributes(node.attributes)}
        </div>

        <div className="entity-provenance">
          <span 
            className="trust-badge"
            style={{ backgroundColor: trustTierColors[node.provenance.trust_tier] }}
            title={trustTierLabels[node.provenance.trust_tier]}
          >
            {trustTierLabels[node.provenance.trust_tier]}
          </span>
          <div className="entity-sources">
            {node.provenance.source_urls.map((url: string, i: number) => (
              <a
                key={i}
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="source-link"
                title={url}
              >
                Source {i + 1}
              </a>
            ))}
          </div>
          {node.provenance.citation_snippet && (
            <p className="citation-snippet">
              "{node.provenance.citation_snippet}"
            </p>
          )}
        </div>
      </div>

      {availableDrilldowns.length > 0 && (
        <div className="entity-drilldowns">
          <h4>Explore further</h4>
          <div className="drilldown-buttons">
            {availableDrilldowns.map((drilldown) => (
              <button
                key={drilldown.dimension_key}
                className="drilldown-btn"
                onClick={() => onDrillDown(drilldown.dimension_key, drilldown.prompt_template)}
              >
                {drilldown.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {availableActions.length > 0 && (
        <div className="entity-actions">
          {availableActions.map((action) => (
            <button
              key={action.action_id}
              className={`action-btn ${action.requires_approval ? 'requires-approval' : ''}`}
            >
              {action.label}
              {action.requires_approval && <span className="approval-lock">🔒</span>}
            </button>
          ))}
        </div>
      )}
    </article>
  );
};