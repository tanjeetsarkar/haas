/**
 * ChecklistCard - Layer 5/6 component for actionable checklists and inquiry drafts
 */

import React from 'react';
import { AtlasLayerNode } from '@haas/sdk';
import './ChecklistCard.css';

interface ChecklistCardProps {
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

export const ChecklistCard: React.FC<ChecklistCardProps> = ({
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

  // Checklist items from attributes
  const checklistItems = (node.attributes.checklist_items as unknown as Array<{
    id: string;
    text: string;
    completed?: boolean;
    required?: boolean;
  }>) || [];

  const otherAttrs = Object.entries(node.attributes)
    .filter(([k]) => k !== 'checklist_items')
    .filter(([_, v]) => v !== null);

  return (
    <article className="checklist-card" data-node-id={node.node_id}>
      <div className="checklist-header">
        <h3 className="checklist-title">{node.title}</h3>
        {node.subtitle && <p className="checklist-subtitle">{node.subtitle}</p>}
      </div>

      <div className="checklist-body">
        {otherAttrs.length > 0 && (
          <div className="checklist-meta">
            {otherAttrs.map(([key, value]) => (
              <div key={key} className="meta-item">
                <span className="meta-label">{key.replace(/_/g, ' ')}</span>
                <span className="meta-value">{String(value)}</span>
              </div>
            ))}
          </div>
        )}

        {checklistItems.length > 0 && (
          <div className="checklist-items">
            <h4>Checklist</h4>
            <ul>
              {checklistItems.map((item) => (
                <li key={item.id} className={`checklist-item ${item.completed ? 'completed' : ''} ${item.required ? 'required' : ''}`}>
                  <label>
                    <input
                      type="checkbox"
                      checked={item.completed || false}
                      readOnly
                    />
                    <span className="item-text">{item.text}</span>
                    {item.required && <span className="required-badge">Required</span>}
                  </label>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="checklist-provenance">
          <span 
            className="trust-badge"
            style={{ backgroundColor: trustTierColors[node.provenance.trust_tier] }}
            title={trustTierLabels[node.provenance.trust_tier]}
          >
            {trustTierLabels[node.provenance.trust_tier]}
          </span>
          <div className="checklist-sources">
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
        <div className="checklist-drilldowns">
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
        <div className="checklist-actions">
          {availableActions.map((action) => (
            <button
              key={action.action_id}
              className={`action-btn ${action.requires_approval ? 'requires-approval' : ''} primary`}
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