/**
 * Atlas Explorer - Main container component for HaaS Atlas layered exploration
 * Implements Miller Columns / Drill-Down Stacks pattern for progressive disclosure
 */

import React, { useState, useCallback, useEffect, useRef } from 'react';
import { AtlasSessionManager, AtlasLayerEnvelope, AtlasRunEvent, AtlasLayerNode } from '@haas/sdk';
import { MillerColumn } from './MillerColumn';
import { HITLApprovalDrawer } from './HITLApprovalDrawer';
import './AtlasExplorer.css';

interface AtlasExplorerProps {
  sessionManager: AtlasSessionManager;
  initialQuery?: string;
  onError?: (error: Error) => void;
}

interface ColumnState {
  layerDepth: number;
  envelope: AtlasLayerEnvelope;
  parentNodeId: string | null;
  threadId: string;
  branchVersion: number;
  runId: string;
  isLoading: boolean;
}

export const AtlasExplorer: React.FC<AtlasExplorerProps> = ({
  sessionManager,
  initialQuery,
  onError,
}) => {
  const [columns, setColumns] = useState<ColumnState[]>([]);
  const [hitlPending, setHitlPending] = useState<{
    actionDigest: string;
    destination: string;
    args: Record<string, unknown>;
    runId: string;
  } | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const currentRunIdRef = useRef<string | null>(null);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (unsubscribeRef.current) {
        unsubscribeRef.current();
      }
    };
  }, []);

  const handleEvent = useCallback((event: AtlasRunEvent) => {
    console.log('Atlas event:', event.type, event.data);

    switch (event.type) {
      case 'output_delta': {
        if (event.data.output_delta?.envelope) {
          const envelope = event.data.output_delta.envelope;
          const layerDepth = envelope.layer_depth;
          
          // Update or add column for this layer
          setColumns(prev => {
            const existingIndex = prev.findIndex(c => c.layerDepth === layerDepth);
            const newColumn: ColumnState = {
              layerDepth,
              envelope,
              parentNodeId: envelope.parent_node_id,
              threadId: '', // Will be set from run context
              branchVersion: 0, // Will be set from run context
              runId: currentRunIdRef.current || '',
              isLoading: false,
            };

            if (existingIndex >= 0) {
              const updated = [...prev];
              updated[existingIndex] = newColumn;
              // Remove any columns deeper than this one (sibling exploration)
              return updated.slice(0, existingIndex + 1);
            } else {
              return [...prev, newColumn];
            }
          });
        }
        break;
      }

      case 'run_completed': {
        // Run completed, stop loading
        setColumns(prev => prev.map(c => ({ ...c, isLoading: false })));
        if (unsubscribeRef.current) {
          unsubscribeRef.current();
          unsubscribeRef.current = null;
        }
        break;
      }

      case 'run_error': {
        const errorMsg = event.data.run_error?.error || 'Run failed';
        setGlobalError(errorMsg);
        onError?.(new Error(errorMsg));
        setColumns(prev => prev.map(c => ({ ...c, isLoading: false })));
        break;
      }

      case 'hitl_pending': {
        if (event.data.hitl_pending) {
          setHitlPending({
            actionDigest: event.data.hitl_pending.action_digest,
            destination: event.data.hitl_pending.destination,
            args: event.data.hitl_pending.args,
            runId: currentRunIdRef.current || '',
          });
        }
        break;
      }

      case 'hitl_approved': {
        setHitlPending(null);
        break;
      }

      case 'tool_dispatched':
      case 'tool_result':
        // Tool progress events - could show progress indicators
        break;
    }
  }, [onError]);

  const startExploration = useCallback(async (query: string) => {
    setGlobalError(null);
    setColumns([]);
    
    try {
      const { threadId, runId, unsubscribe } = await sessionManager.startRootExploration(query, handleEvent);
      currentRunIdRef.current = runId;
      unsubscribeRef.current = unsubscribe;
      
      // Set initial loading state for layer 1
      setColumns([{
        layerDepth: 1,
        envelope: {
          schema_version: 'atlas-layer.v1',
          layer_depth: 1,
          parent_node_id: null,
          domain_category: 'general',
          nodes: [],
          available_drilldowns: [],
          available_actions: [],
        },
        parentNodeId: null,
        threadId,
        branchVersion: 0,
        runId,
        isLoading: true,
      }]);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : 'Failed to start exploration');
      onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  }, [sessionManager, handleEvent, onError]);

  const handleDrillDown = useCallback(async (
    parentColumn: ColumnState,
    node: AtlasLayerNode,
    dimensionKey: string,
    prompt: string
  ) => {
    setGlobalError(null);
    
    try {
      const { branchedThreadId, runId, unsubscribe } = await sessionManager.drillDownLayer({
        threadId: parentColumn.threadId,
        parentBranchVersion: parentColumn.branchVersion,
        parentNodeId: node.node_id,
        dimensionKey,
        prompt,
      }, handleEvent);
      
      currentRunIdRef.current = runId;
      unsubscribeRef.current = unsubscribe;
      
      // Add loading state for new layer
      setColumns(prev => {
        const newDepth = parentColumn.layerDepth + 1;
        const existingIndex = prev.findIndex(c => c.layerDepth === newDepth);
        const newColumn: ColumnState = {
          layerDepth: newDepth,
          envelope: {
            schema_version: 'atlas-layer.v1',
            layer_depth: newDepth,
            parent_node_id: node.node_id,
            domain_category: parentColumn.envelope.domain_category,
            nodes: [],
            available_drilldowns: [],
            available_actions: [],
          },
          parentNodeId: node.node_id,
          threadId: branchedThreadId,
          branchVersion: parentColumn.branchVersion + 1,
          runId,
          isLoading: true,
        };

        if (existingIndex >= 0) {
          const updated = [...prev];
          updated[existingIndex] = newColumn;
          return updated.slice(0, existingIndex + 1);
        } else {
          return [...prev, newColumn];
        }
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : 'Failed to drill down');
      onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  }, [sessionManager, handleEvent, onError]);

  const handleHitlApprove = useCallback(async (approved: boolean, rejectionReason?: string) => {
    if (!hitlPending) return;
    
    try {
      await sessionManager.resumeHitlRun(
        hitlPending.runId,
        approved,
        hitlPending.actionDigest,
        rejectionReason
      );
      setHitlPending(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : 'Failed to resume run');
      onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  }, [sessionManager, hitlPending, onError]);

  // Start initial exploration if query provided
  useEffect(() => {
    if (initialQuery) {
      startExploration(initialQuery);
    }
  }, [initialQuery, startExploration]);

  return (
    <div className="atlas-explorer">
      {globalError && (
        <div className="atlas-error-banner" role="alert">
          <span>{globalError}</span>
          <button onClick={() => setGlobalError(null)} aria-label="Dismiss error">×</button>
        </div>
      )}

      <div className="atlas-toolbar">
        <h1>HaaS Atlas</h1>
        <div className="atlas-query-input">
          <input
            type="text"
            placeholder="Enter query (e.g., 'Cancer hospitals in Mumbai')"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && e.currentTarget.value.trim()) {
                startExploration(e.currentTarget.value.trim());
                e.currentTarget.value = '';
              }
            }}
            aria-label="Exploration query"
          />
          <button 
            onClick={() => {
              const input = document.querySelector('.atlas-query-input input') as HTMLInputElement;
              if (input?.value.trim()) {
                startExploration(input.value.trim());
                input.value = '';
              }
            }}
            disabled={columns.some(c => c.isLoading)}
          >
            Explore
          </button>
        </div>
      </div>

      <div className="atlas-columns" role="region" aria-label="Exploration layers">
        {columns.map((column, index) => (
          <MillerColumn
            key={column.layerDepth}
            column={column}
            index={index}
            onDrillDown={handleDrillDown}
            isLast={index === columns.length - 1}
          />
        ))}
        {columns.length === 0 && (
          <div className="atlas-empty-state">
            <p>Enter a query above to start exploring</p>
            <p className="atlas-hint">Try: "Cancer hospitals in Mumbai", "Universities in Berlin", "Laptops under $1000"</p>
          </div>
        )}
      </div>

      {hitlPending && (
        <HITLApprovalDrawer
          actionDigest={hitlPending.actionDigest}
          destination={hitlPending.destination}
          args={hitlPending.args}
          onApprove={handleHitlApprove}
          onDismiss={() => setHitlPending(null)}
        />
      )}
    </div>
  );
};