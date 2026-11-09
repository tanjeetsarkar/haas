import { useEffect, useState } from 'react';

/** Navigation configuration only; never authentication or a role switch. */
export function independentApproverOrigin(value: string | undefined, currentOrigin: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
      || url.username || url.password || url.search || url.hash || url.pathname !== '/'
      || url.origin === currentOrigin) return null;
    return url.origin;
  } catch { return null; }
}

const developmentBuild = (import.meta as ImportMeta & { env?: { DEV?: boolean } }).env?.DEV === true;

export function AtlasOperatorLink({ batchId, configuredOrigin, development = developmentBuild }: {
  batchId: string; configuredOrigin?: string; development?: boolean;
}) {
  const [devSubmitter, setDevSubmitter] = useState(false);
  useEffect(() => {
    if (!development) return;
    let active = true;
    void fetch('/__atlas/dev-session', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(async response => response.ok ? response.json() as Promise<unknown> : null)
      .then(value => {
        if (active && value && typeof value === 'object' && 'role' in value && value.role === 'submitter'
          && 'developmentOnly' in value && value.developmentOnly === true) setDevSubmitter(true);
      }).catch(() => undefined);
    return () => { active = false; };
  }, [development]);
  const configured = independentApproverOrigin(configuredOrigin, window.location.origin);
  const origin = configured ?? (development && devSubmitter
    ? independentApproverOrigin('http://127.0.0.1:4318', window.location.origin) : null);
  if (!origin) return <p>Use your independently configured operator approver window and enter batch id <code>{batchId}</code>.
    No independent approver origin is configured here.</p>;
  const url = new URL('/', origin);
  url.search = new URLSearchParams({ atlasApprover: '1', batch: batchId }).toString();
  return <p><a href={url.href} target="_blank" rel="noopener noreferrer">Open independent operator review for {batchId}</a>
    <small>Opens a separate operator window only when clicked. The server verifies identity and separate-authorizer constraints; this link grants no authority.</small></p>;
}
