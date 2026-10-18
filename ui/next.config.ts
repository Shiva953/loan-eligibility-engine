// The page is a static export: plain files that `make ui` serves next to its API stand-ins, or
// any static host once deployed. Its three calls go wherever /config.js points (the local
// stand-ins, or the Function URLs and the bucket). `next dev` has no such server, so in
// development it forwards /config.js and /api/* to `make ui-api` on port 8090.
import type { NextConfig } from 'next';
import { PHASE_DEVELOPMENT_SERVER } from 'next/constants';

const API = `http://127.0.0.1:${process.env.UI_HOST_PORT ?? '8090'}`;

export default function config(phase: string): NextConfig {
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      reactStrictMode: true,
      turbopack: { root: process.cwd() },
      async rewrites() {
        return [
          { source: '/config.js', destination: `${API}/config.js` },
          { source: '/api/:path*', destination: `${API}/api/:path*` },
        ];
      },
    };
  }
  return {
    output: 'export',
    reactStrictMode: true,
    images: { unoptimized: true },
    turbopack: { root: process.cwd() }, // npm runs this in ui/; don't guess from stray lockfiles
  };
}
