import type { NextConfig } from "next";

// The browser only ever talks to this app; /api is proxied to the FastAPI backend so session cookies are
// first-party and no API secret ever reaches client code.
const API_URL = process.env.MERIDIAN_API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/:path*` }];
  },
};

export default config;
