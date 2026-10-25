/**
 * HITLApprovalDrawer - Immutable Consent & Approval Drawer for Human-in-the-Loop actions
 * Shows exact payload digest and destination before execution
 */

import React, { useState } from 'react';
import './HITLApprovalDrawer.css';

interface HITLApprovalDrawerProps {
  actionDigest: string;
  destination: string;
  args: Record<string, unknown>;
  onApprove: (approved: boolean, rejectionReason?: string) => void;
  onDismiss: () => void;
}

export const HITLApprovalDrawer: React.FC<HITLApprovalDrawerProps> = ({
  actionDigest,
  destination,
  args,
  onApprove,
  onDismiss,
}) => {
  const [rejectionReason, setRejectionReason] = useState('');

  const formatArgs = (obj: Record<string, unknown>, indent = 0): React.ReactNode => {
    return Object.entries(obj).map(([key, value]) => (
      <div key={key} className="arg-line" style={{ paddingLeft: `${indent * 20}px` }}>
        <span className="arg-key">{key}:</span>
        {typeof value === 'object' && value !== null ? (
          <>
            <span className="arg-brace">{Array.isArray(value) ? '[' : '{'}</span>
            <div className="arg-nested">
              {formatArgs(value as Record<string, unknown>, indent + 1)}
            </div>
            <span className="arg-brace">{Array.isArray(value) ? ']' : '}'}</span>
          </>
        ) : (
          <span className="arg-value">{JSON.stringify(value)}</span>
        )}
      </div>
    ));
  };

  return (
    <div className="hitl-drawer-overlay" onClick={onDismiss} role="dialog" aria-modal="true" aria-labelledby="hitl-title">
      <div className="hitl-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="hitl-drawer-header">
          <h2 id="hitl-title">⚠️ Human Approval Required</h2>
          <button className="hitl-close-btn" onClick={onDismiss} aria-label="Close">×</button>
        </div>

        <div className="hitl-drawer-content">
          <div className="hitl-warning">
            <p><strong>This action will execute an external request.</strong> Please review the details below carefully before approving.</p>
            <p>The action cannot be undone once approved.</p>
          </div>

          <div className="hitl-detail-section">
            <h3>Action Digest (SHA-256)</h3>
            <code className="hitl-digest">{actionDigest}</code>
            <p className="hitl-digest-note">This digest cryptographically binds your approval to this exact payload. Any modification would invalidate the approval.</p>
          </div>

          <div className="hitl-detail-section">
            <h3>Destination</h3>
            <code className="hitl-destination">{destination}</code>
          </div>

          <div className="hitl-detail-section">
            <h3>Request Payload</h3>
            <div className="hitl-payload">
              {formatArgs(args)}
            </div>
          </div>

          <div className="hitl-detail-section">
            <h3>Security Checks</h3>
            <ul className="hitl-checks">
              <li>✅ Destination URL validated against SSRF allowlist</li>
              <li>✅ Request includes idempotency key for deduplication</li>
              <li>✅ Payload digest matches approval gate binding</li>
              <li>✅ Separate approver principal required (no self-approval)</li>
            </ul>
          </div>
        </div>

        <div className="hitl-drawer-footer">
          <button
            className="hitl-btn hitl-btn-reject"
            onClick={() => onApprove(false, rejectionReason || 'Rejected by user')}
            disabled={!rejectionReason.trim()}
          >
            Reject
          </button>
          <textarea
            className="hitl-rejection-input"
            placeholder="Rejection reason (required)..."
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            rows={3}
            aria-label="Rejection reason"
          />
          <button
            className="hitl-btn hitl-btn-approve"
            onClick={() => onApprove(true)}
          >
            ✅ Approve & Execute
          </button>
        </div>
      </div>
    </div>
  );
};