import type { NextConfig } from "next";

// The browser only ever talks to this app; /api is proxied to the FastAPI backend so session cookies are
// first-party and no API secret ever reaches client code.
const API_URL = process.env.MERIDIAN_API_URL ?? "http://localhost:8000";
const prod = process.env.NODE_ENV === "production";

// Everything the app loads comes from its own origin. Next.js needs inline scripts for hydration (and eval in
// development only); the theme script in layout.tsx is inline too.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${prod ? "" : " 'unsafe-eval'"}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "worker-src 'self'",
  "manifest-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  ...(prod ? [{ key: "Strict-Transport-Security", value: "max-age=31536000" }] : []),
];

const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/:path*` }];
  },
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      // The service worker must not be cached for long, so updates reach browsers.
      { source: "/sw.js", headers: [{ key: "Cache-Control", value: "no-cache" }] },
    ];
  },
};

export default config;
