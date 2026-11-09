# `@haas/atlas-explorer-client`

Private TypeScript package for Atlas Explorer domain contracts and the
`AtlasSessionManager` facade. Version `0.1.0`; its transitional peer range is
`@haas/sdk` `^0.1.0 || ^1.0.0`.

The package owns its TypeScript, Vitest, and typecheck dependencies and can be
built or tested independently. It has no workspace dependency and imports the
public `@haas/sdk` package for its client and run types. The facade preserves
the behavior of the former SDK implementation; it does not assert server
routes, event-schema compatibility, authentication, or service transport.

## Local verification against the current SDK

From the repository root, build and pack the current public SDK, install the
tarball as this package's local peer, then run the package gates:

```sh
cd haas_sdk_ts
npm run build
npm pack --pack-destination /tmp
cd ../services/atlas-explorer/client-ts
npm ci --legacy-peer-deps
npm install --no-save /tmp/haas-sdk-0.1.0.tgz
npm test
npm run build
npm run typecheck
```

The package is private. These commands are for local contract verification;
they do not publish or release either package.

The Atlas Explorer service remains a health-only skeleton. This client package
does not imply Atlas routes, authentication, persistence, or a functioning
service transport; authentication is deferred.
