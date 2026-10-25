/**
 * SpecialistProfile - Layer 4 component for specialist/doctor profiles
 */

import React from 'react';
import { AtlasLayerNode } from '@haas/sdk';
import './SpecialistProfile.css';

interface SpecialistProfileProps {
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

export const SpecialistProfile: React.FC<SpecialistProfileProps> = ({
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

  const specialistAttrs = Object.entries(node.attributes).filter(([_, v]) => v !== null);

  return (
    <article className="specialist-profile" data-node-id={node.node_id}>
      <div className="specialist-header">
        <div className="specialist-avatar">
          {node.attributes.initials || node.title.charAt(0)}
        </div>
        <div className="specialist-info">
          <h3 className="specialist-name">{node.title}</h3>
          {node.subtitle && <p className="specialist-role">{node.subtitle}</p>}
        </div>
      </div>

      <div className="specialist-body">
        <div className="specialist-attributes">
          {specialistAttrs.map(([key, value]) => (
            <div key={key} className="specialist-attr">
              <span className="attr-label">{key.replace(/_/g, ' ')}</span>
              <span className="attr-value">{String(value)}</span>
            </div>
          ))}
        </div>

        <div className="specialist-provenance">
          <span 
            className="trust-badge"
            style={{ backgroundColor: trustTierColors[node.provenance.trust_tier] }}
            title={trustTierLabels[node.provenance.trust_tier]}
          >
            {trustTierLabels[node.provenance.trust_tier]}
          </span>
          <div className="specialist-sources">
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
        <div className="specialist-drilldowns">
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
      )}

      {availableActions.length > 0 && (
        <div className="specialist-actions">
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