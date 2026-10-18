import type { Metadata, Viewport } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import Script from 'next/script';
import type { ReactNode } from 'react';
import './globals.css';

// Geist is downloaded at build time and served with the page: no request to Google at runtime.
const geist = Geist({ subsets: ['latin'], variable: '--font-geist', display: 'swap' });
const geistMono = Geist_Mono({ subsets: ['latin'], variable: '--font-geist-mono', display: 'swap' });

export const metadata: Metadata = {
  title: 'Loan Eligibility Engine',
  description: 'Upload a customer list and see which loans each customer qualifies for, in seconds.',
};

export const viewport: Viewport = { themeColor: '#ffffff', colorScheme: 'light' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en-IN" className={`${geist.variable} ${geistMono.variable}`}>
      <body>
        {/* Where the page's calls go: local stand-ins, or the deployed stack (scripts/ui_server.py). */}
        <Script src="/config.js" strategy="beforeInteractive" />
        {children}
      </body>
    </html>
  );
}
