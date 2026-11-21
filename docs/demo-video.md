# Local demo video

Open `artifacts/supplier-chaser-demo.mp4` in a browser or video player. This is an
84-second, silent, captioned walkthrough assembled from actual fixture-demo screenshots.
Every frame labels the synthetic data and fixture mailbox. It is not a recording of
the live supplier exchange and does not demonstrate the new source-mapping screen.

The workflow shows a quantity discrepancy, its source evidence, a supplier question,
an amended confirmation, a recorded fixture reply and reconciliation. A resolved
confirmation discrepancy still does not establish physical delivery.

To rebuild from the captured frames, install FFmpeg and run:

```sh
pixi run python scripts/package_video.py
```

To recapture the storyboard after following `docs/demo-runbook.md`, run
`node scripts/capture.mjs video` first (requires local Google Chrome and Node).
Screenshots and video stay in the ignored `artifacts/` directory.

Live portal reads, SMTP sends, IMAP receipt and the configured model have been exercised
separately during local setup. Live replies still require identity review. Unsupported
documents and rejected model extractions remain unresolved; there is no public deployment.
