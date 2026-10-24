/**
 * PricingTierCard - Layer 3 component for pricing/scheme coverage breakdown
 */

import React from 'react';
import { AtlasLayerNode } from '@haas/sdk';
import './PricingTierCard.css';

interface PricingTierCardProps {
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

export const PricingTierCard: React.FC<PricingTierCardProps> = ({
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

  // Extract pricing-specific attributes
  const pricingAttrs = Object.entries(node.attributes).filter(([_, v]) => v !== null);

  return (
    <article className="pricing-tier-card" data-node-id={node.node_id}>
      <div className="pricing-card-header">
        <h3 className="pricing-title">{node.title}</h3>
        {node.subtitle && <p className="pricing-subtitle">{node.subtitle}</p>}
      </div>

      <div className="pricing-card-body">
        <div className="pricing-tiers">
          {pricingAttrs.map(([key, value]) => (
            <div key={key} className="pricing-tier">
              <span className="tier-name">{key.replace(/_/g, ' ')}</span>
              <span className="tier-value">{String(value)}</span>
            </div>
          ))}
        </div>

        <div className="pricing-provenance">
          <span 
            className="trust-badge"
            style={{ backgroundColor: trustTierColors[node.provenance.trust_tier] }}
            title={trustTierLabels[node.provenance.trust_tier]}
          >
            {trustTierLabels[node.provenance.trust_tier]}
          </span>
          <div className="pricing-sources">
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

        {/* Scheme coverage badge if present */}
        {node.attributes.scheme_coverage && (
          <div className="scheme-coverage">
            <span className="scheme-label">Scheme Coverage:</span>
            <span className="scheme-value">{node.attributes.scheme_coverage}</span>
          </div>
        )}
      </div>

      {availableDrilldowns.length > 0 && (
        <div className="pricing-drilldowns">
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
        <div className="pricing-actions">
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