import type { ReactNode } from "react";

/** 一套描边图标：24 网格、1.75 描边、currentColor，替代散落各处的 emoji。 */
const PATHS = {
  paper: <><path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></>,
  play: <path d="M8 5.5v13l10-6.5z" />,
  call: <path d="M5 12h13M13 6.5l5.5 5.5-5.5 5.5" />,
  result: <path d="M5 4v7a4 4 0 0 0 4 4h10M15 10.5l4.5 4.5-4.5 4.5" />,
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  download: <path d="M12 4v11M7 10.5l5 5 5-5M5 20h14" />,
  upload: <path d="M12 16V4M7 8.5l5-5 5 5M5 20h14" />,
  box: <><path d="M20.5 7.5 12 3 3.5 7.5v9L12 21l8.5-4.5z" /><path d="m3.5 7.5 8.5 4.5 8.5-4.5M12 12v9" /></>,
  shield: <><path d="M12 3 5 5.8V11c0 4.6 3 8 7 10 4-2 7-5.4 7-10V5.8z" /><path d="m9 12 2.2 2.2L15.5 10" /></>,
  target: <><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="4.5" /><circle cx="12" cy="12" r="0.75" fill="currentColor" /></>,
  alert: <><path d="M10.3 4.2 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z" /><path d="M12 9.5v4M12 17h.01" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  close: <path d="M17.5 6.5l-11 11M6.5 6.5l11 11" />,
  flag: <path d="M5.5 21V4M5.5 4H17l-2.5 4.25L17 12.5H5.5" />,
  chat: <path d="M20 11.5a7.5 7.5 0 0 1-11 6.6L4.5 19.5l1.4-4.2A7.5 7.5 0 1 1 20 11.5z" />,
  list: <path d="M9 6.5h11M9 12h11M9 17.5h11M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01" />,
  chevron: <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />,
  stop: <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" />,
  pencil: <path d="M4.5 19.5h3.75L19 8.75 15.25 5 4.5 15.75zM13 7.25 16.75 11" />,
  trash: <path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.9 12.5h9.2L17.5 7M10 11v5M14 11v5" />,
  info: <><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5.5M12 7.5h.01" /></>,
  dot: <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none" />,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" /></>,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 3v2M12 19v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M3 12h2M19 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></>,
  moon: <path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" />,
  monitor: <><rect x="3" y="4.5" width="18" height="12" rx="2" /><path d="M8.5 20h7M12 16.5V20" /></>,
  arrowLeft: <path d="M15 6.5 9.5 12 15 17.5" />,
  arrowRight: <path d="m9 6.5 5.5 5.5L9 17.5" />,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export default function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.75}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden focusable="false" className={className}>
      {PATHS[name]}
    </svg>
  );
}
