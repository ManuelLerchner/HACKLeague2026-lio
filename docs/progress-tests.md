# Progress refresh regression checks

Run `node scripts/test_progress.cjs` from any directory. The script uses Node's standard library
and a small DOM stub; it needs no browser, network connection, app server, credentials or packages.

It verifies that completed work refreshes untouched pages, unsaved edits show an explicit refresh
button instead of reloading, hidden pages do not poll, edits made during submission are kept,
and an open evidence preview prevents refresh until the dialog closes.
This checks client-side refresh behavior; it does not substitute for visual browser QA.
