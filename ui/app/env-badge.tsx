'use client';

// Which backend the page talks to, read from /config.js after load: the deployed AWS stack or
// `make ui`'s local stand-ins. Shown so a viewer knows what a demo is running against.

import { useEffect, useState } from 'react';
import { config } from '../lib/api.ts';

export default function EnvBadge() {
  const [env, setEnv] = useState<{ aws: boolean; region?: string } | null>(null);
  useEffect(() => {
    const c = config();
    setEnv({ aws: c.target === 'aws', region: c.region });
  }, []);
  if (!env) return null;
  return (
    <span className={`env${env.aws ? ' is-aws' : ''}`} title={env.aws ? 'Connected to the deployed AWS stack' : 'Connected to the local stand-ins'}>
      <span className="env-dot" aria-hidden="true" />
      {env.aws ? `Live on AWS${env.region ? ` · ${env.region}` : ''}` : 'Local demo'}
    </span>
  );
}
