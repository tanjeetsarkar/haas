/**
 * Atlas Demo App - Example usage of HaaS Atlas components
 */

import { useEffect, useState } from 'react';
import { createClientFromEnv, createAtlasSessionManager } from '@haas/sdk';
import { AtlasExplorer } from './components/atlas';
import './App.css';

function App() {
  const [sessionManager, setSessionManager] = useState<ReturnType<typeof createAtlasSessionManager> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Initialize HaaS client from environment
    const client = createClientFromEnv();
    const manager = createAtlasSessionManager(client);
    setSessionManager(manager);
  }, []);

  if (!sessionManager) {
    return (
      <div className="app-loading">
        <div className="spinner" />
        <p>Initializing HaaS Atlas...</p>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>HaaS Atlas Demo</h1>
        <p className="app-subtitle">Progressive disclosure exploration with provenance-verified entities</p>
      </header>
      <main className="app-main">
        <AtlasExplorer
          sessionManager={sessionManager}
          initialQuery="Cancer hospitals in Mumbai"
          onError={(err) => setError(err.message)}
        />
      </main>
      {error && (
        <div className="app-error" role="alert">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}
    </div>
  );
}

export default App;