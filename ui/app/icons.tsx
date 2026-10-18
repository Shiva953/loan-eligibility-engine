// A few inline icons, drawn on one 24px grid with the same 1.75 stroke, so they read as a set.
import type { SVGProps } from 'react';

const base = {
  width: 24, height: 24, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.75, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true,
} as const;

export function LogoMark({ size = 64, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className={className} role="img" aria-label="Loan Eligibility Engine">
      <defs>
        <linearGradient id="logo-fill" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#3566f2" />
          <stop offset="1" stopColor="#1d3fc0" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill="url(#logo-fill)" />
      <path d="M19 33.5l8.5 8.5L45 24.5" stroke="#fff" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <circle cx="47.5" cy="45.5" r="4.5" fill="#43e397" />
    </svg>
  );
}

export function IconUpload(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 15V4" /><path d="M7.5 8.5 12 4l4.5 4.5" />
      <path d="M4 14.5v3A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5v-3" />
    </svg>
  );
}

export function IconFile(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M14 3H7.5A2.5 2.5 0 0 0 5 5.5v13A2.5 2.5 0 0 0 7.5 21h9a2.5 2.5 0 0 0 2.5-2.5V8z" />
      <path d="M14 3v5h5" /><path d="M9 13h6" /><path d="M9 16.5h4" />
    </svg>
  );
}

export function IconCheck(props: SVGProps<SVGSVGElement>) {
  return <svg {...base} strokeWidth={2.5} {...props}><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>;
}

export function IconAlert(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="9" /><path d="M12 7.5v5" /><path d="M12 16.2v.1" />
    </svg>
  );
}

export function IconRows(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="4" y="4" width="16" height="16" rx="2.5" /><path d="M4 9.5h16" /><path d="M4 14.5h16" /><path d="M10 9.5V20" />
    </svg>
  );
}

export function IconScale(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 4v16" /><path d="M7 20h10" /><path d="M5 7h14" />
      <path d="m5 7-2.5 6a2.5 2.5 0 0 0 5 0z" /><path d="m19 7-2.5 6a2.5 2.5 0 0 0 5 0z" />
    </svg>
  );
}

export function IconMail(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="3" y="5" width="18" height="14" rx="2.5" /><path d="m4 7 8 6 8-6" />
    </svg>
  );
}

export function IconCloud(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M7 18.5h10a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 7.1 9.6 4.5 4.5 0 0 0 7 18.5z" /><path d="M12 15.5v-5" /><path d="m9.75 12.5 2.25-2.25 2.25 2.25" />
    </svg>
  );
}

export function IconSpark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 3.5 13.9 10l6.6 2-6.6 2L12 20.5 10.1 14l-6.6-2 6.6-2z" />
    </svg>
  );
}

export function IconBank(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M3.5 9.5 12 4.5l8.5 5" /><path d="M5 10v7.5" /><path d="M9.67 10v7.5" /><path d="M14.33 10v7.5" /><path d="M19 10v7.5" /><path d="M3.5 20h17" />
    </svg>
  );
}
