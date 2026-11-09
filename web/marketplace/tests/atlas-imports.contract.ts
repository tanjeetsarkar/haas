import {
  AtlasSessionManager,
  createAtlasSessionManager,
  type AtlasEventHandler,
  type AtlasLayerEnvelope,
  type AtlasLayerNode,
  type AtlasRunEvent,
  type DrilldownRequest,
} from '@haas/atlas-explorer-client';
import { createClientFromEnv } from '@haas/sdk';

export function createAtlasManagerContract(): AtlasSessionManager {
  return createAtlasSessionManager(createClientFromEnv());
}

export type {
  AtlasEventHandler,
  AtlasLayerEnvelope,
  AtlasLayerNode,
  AtlasRunEvent,
  DrilldownRequest,
};
