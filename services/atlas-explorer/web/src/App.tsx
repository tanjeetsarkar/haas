import { useState } from 'react';
import { AtlasPreview } from './components/AtlasPreview';
import { AtlasWorkspace } from './components/AtlasWorkspace';
import { createBrowserAtlasAdapter } from './liveAdapter';
import { createBrowserApproverAdapter } from './approverAdapter';
import { AtlasApproverPage } from './components/AtlasApproverPage';
import './App.css';

function App() {
  if (new URLSearchParams(window.location.search).get('atlasApprover') === '1') return <OperatorApproverApp />;
  if (new URLSearchParams(window.location.search).get('atlasPreview') === '1') {
    return <AtlasPreview onExit={() => window.location.assign(window.location.pathname)} />;
  }
  return <LiveAtlasApp />;
}

function OperatorApproverApp() {
  const [adapter] = useState(createBrowserApproverAdapter);
  return <AtlasApproverPage adapter={adapter} />;
}

function LiveAtlasApp() {
  const [adapter] = useState(createBrowserAtlasAdapter);
  const approverOrigin = document.querySelector<HTMLMetaElement>('meta[name="atlas-approver-origin"]')?.content;
  return <AtlasWorkspace adapter={adapter} approverOrigin={approverOrigin} />;
}

export default App;