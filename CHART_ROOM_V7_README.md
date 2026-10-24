# Vicissitude Chart Room V7 — Development branch

This is a **development branch**, not the live Version 6 backend. No Render deploy or Apple submission is implied.

## Added
- Authenticated `POST /api/account/chart-room` to return only the signed-in account's bottle positions, dates, miles, and status (never message text).
- A per-bottle `route_points` list containing real simulator checkpoints. A point is appended when a simulated step actually changes the bottle's position/status.
- Older bottles fall back to recorded journey event coordinates; missing coordinates are **not invented**.

## iOS integration
Use the self-contained Version 7 `ContentView.swift` and `VicissitudeChartRoom.imageset` in the Vicissitude file library under:
`/Vicissitude/Archive/07 Catalog/Chart_Room_V7_Development_Package.zip`

The Swift view has animated golden dots, bottle selection, account-scoped on-device caching, and a no-purchase menu entry.

## Before deploying
1. Compile the iPhone app in Xcode; code is Swift-syntax-checked but **not iOS typechecked or device-tested**.
2. Run backend tests for movement and StoreKit purchase flows.
3. Verify the existing live backend remains healthy and that `route_points` are saved after actual current-worker calculations.
4. Calibrate or replace the artistically illustrated world chart with geographically faithful coordinates before calling the display a precise voyage map. In V7 the underlying illustration is decorative.
5. Independently implement and test the 24-hour unattended scheduler; this branch only adds checkpoint recording and the authenticated chart API. **It does not yet automatically advance all inactive bottles.**
6. Upgrade the database and verify backup/retention; independent of this branch.

No paid Render job was created.

## Live ocean-current smoke test — October 23, 2026
- GitHub Actions run: https://github.com/mrmdeal1/message-in-a-bottle/actions/runs/40044813160
- Result: **passed**. The production current-worker `/health` returned 200; read-only `/current` at latitude 25.0, longitude -85.0 and date 2026-10-20 returned finite realistic east/north surface-current components.
- Read-only `/current` request took about 30 seconds (Render request log: ~30,316 ms). Optimize/budget multiple lookups before enabling cron for every bottle.
- No customer records were created, updated, or read by this smoke test; no cron job was created.
- Unit regression tests for the scheduler passed in GitHub Actions run https://github.com/mrmdeal1/message-in-a-bottle/actions/runs/40044639452, including dry run, concurrency, 24h eligibility and error rollback behavior.
- These checks do **not** constitute an end-to-end scheduled live bottle update through the production database; that still needs separate staging verification.
