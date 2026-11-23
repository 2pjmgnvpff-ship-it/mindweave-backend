# MindWeave (心络) — 100-Point Multi-Judge Quality Benchmark Rubric

> **Benchmark Standard**: Apple iOS 18/26 HIG & Native Feel × Gleb Kuznetsov Luminous Tactile Direction × Meta AI Personal Agent Architecture × Zero-Hallucination Privacy Security × Real Xcode/arm64 `.ipa` Packaging.
> **Passing Rule**: Every single independent adversarial Judge MUST score **>= 95 / 100** AND emit `VERDICT: PASS` with **ZERO** unresolved critical/major actionable defects. Any score `< 95` or any "can be done better" shortcut triggers an immediate `VERDICT: REJECT` back to execution subagents.

---

## Judge 1 — `judge_ios_ux` (Apple iOS HIG & Native Interaction Nitpicker, 100 pts)

| Criterion | Weight | Pass Criteria |
|---|---:|---|
| 1. Touch Targets & Ergonomics | 15 | Every interactive control provides `>= 44×44pt` effective hit area (using invisible touch expansion where visual chrome is delicate); zero pointer-event blocking; thumb-zone ergonomics. |
| 2. Native Gesture Physics | 25 | Finger-tracked swipe-down-to-dismiss with rubber-band resistance & velocity threshold on bottom sheets & Peek overlay; left-edge swipe-right-to-pop on Cupid Chat; vertical finger scrub on right-edge paper tabs with floating frosted Month Loupe; zero horizontal pan bugs. |
| 3. Haptics, Native Bridge & Media | 15 | `native-bridge.js` cleanly invokes `MWNative` iOS haptics (`light`/`medium`/`selection`/`success`/`warning`), Share Sheet, Clipboard, with graceful Web fallback and zero runtime errors. |
| 4. Safe Area & Keyboard Avoidance | 15 | Proper `env(safe-area-inset-*)`, `visualViewport` keyboard offset (`--kb-inset`) keeping composers above software keyboard, zero clipped headers/docks on `390×844` iPhone viewports. |
| 5. Typography, Contrast & Accessibility | 15 | Zero clipped/sliced scroll edges, WCAG readable contrast, complete ARIA roles/labels, Escape/keyboard navigation, `prefers-reduced-motion` compliance. |
| 6. Real Screenshot & Code Audit | 15 | Verified directly via Playwright screenshots & source inspection. |

## Judge 2 — `judge_gleb_aesthetic` (Gleb Kuznetsov Visual & Motion Critic, 100 pts)

| Criterion | Weight | Pass Criteria |
|---|---:|---|
| 1. Pure Resting Slip Law (Zero Tolerance) | 25 | Every resting slip shows **ONLY big serif original text + tiny date** (plus voice waveform on voice slips, tiny 3D brass/silver tack on pinned, dashed graphite + dotted date on D1 pencil). Zero category pills, zero status tags, zero inline buttons. |
| 2. Gleb Luminous Atmosphere & Depth | 20 | Subsurface-scattered warm peach-apricot ambient sunlight bloom, airy breathing space, frosted milky glass catchlights, multi-layered ambient shadows, zero cheap purple/neon AI tropes. |
| 3. Living Luminous AI Orb & Tri-Dock | 15 | Right AI orb utilizes Gleb `orb_ai_glow.png` + subsurface volumetric aura (tier-aware), balanced symmetrically with tactile voice orb & dark glass capsule. |
| 4. Splash Bokeh & Morph Choreography | 15 | Cinematic `lake_bench_clean.jpg` bokeh splash + frosted pill glass card + smooth Lake→Capsule morph (`#morphActor`) into journal. |
| 5. Tier Storytelling & Portrait Purity | 15 | Gold (champagne/brass), Silver (cool misty slate, `.is-cloaked` zero-photo), Crystal (prismatic ice-blue); Cupid ONLY uses `muse_cupid_a_listen.webp`; 6 papers ONLY in Settings (`cotton` default). |
| 6. Pixel-Perfect Visual Audit | 10 | Every rendered screenshot visually inspected with zero overlap, clipping, or muddy contrast. |

## Judge 3 — `judge_meta_agent` (Meta AI Agent Architect, 100 pts)

| Criterion | Weight | Pass Criteria |
|---|---:|---|
| 1. Streaming Agent Execution Trace | 20 | Backend streams truthful `agent_step` SSE events (real scanned record count, facts/reminders checked, D4 recall hits); UI renders live status settling into collapsible poetic trace pill (`✦ 已查阅 X 页手帐 · 命中 Y 处原话 ▾`). |
| 2. 1-Tap Executable Action Cards | 20 | Backend emits structured `action_proposal` SSE events + real RLS/encrypted `POST /v1/people/{id}/reminders`; UI executes 1-tap creation and transitions to `✓ 已设好提醒`. |
| 3. Voice-First Conversational Cupid | 20 | Hold-to-speak mic directly in Cupid composer (swapping cleanly with send arrow), wired to real `POST /v1/voice/transcribe` (context-aware ASR + homophone/self-correction) streaming straight to Cupid. |
| 4. Proactive Brief & Editable Memory | 15 | Real `GET /v1/people/{id}/brief` surfaced serenely in Cover Vellum Pocket & fresh Cupid chat (zero clutter on resting slips, zero pseudo-science scores) + in-Vellum memory controls (confirm pencil fact, pause/resume reminder, forget fact). |
| 5. Context-Aware ASR & Multi-Event Split | 15 | History-grounded homophone/self-correction + zero-bleed multi-person & multi-event `RecordRow` splitting with atomic 5s undo. |
| 6. D4 BM25 Recall & Tier Constitution | 10 | PersonScope-isolated Chinese BM25 recall, zero-UUID citations, Gold/Silver/Crystal constitutional fidelity + full unit/integration tests. |

## Judge 4 — `judge_backend_security` (Security, Privacy & Red-Team Auditor, 100 pts)

| Criterion | Weight | Pass Criteria |
|---|---:|---|
| 1. RLS & AES-256-GCM Isolation | 25 | Every new DB read/write enforces `UserScope`/`PersonScope`, `FORCE RLS`, and AES-256-GCM AAD (`user_id`, `table`, `column`, `row_id`). |
| 2. Zero-Hallucination & Safety | 20 | `0.00%` cross-person bleed, `0.00%` hallucinated citations, `100%` crisis recall / `0%` hyperbole FP, Silver zero-photo & zero-perspectiveCard enforced end-to-end. |
| 3. Frontend T4 XSS & Modularity | 15 | Zero dynamic `innerHTML` across `web/` (except `dom.js` static SVG); every JS file `<= 500` lines; zero UUID/dev jargon leakage. |
| 4. Clean Architecture & Static Gates | 20 | `make lint`, `make typecheck` (0 MyPy issues), `make arch` (3/3 KEPT), `make openapi` clean. |
| 5. Full Automated Test & Eval Pass | 20 | `make test` 100% PASS, `make eval` 66/66 100%, `node --test` 100% PASS, Playwright E2E 100% PASS with 0 console/page/5xx errors. |

## Judge 5 — `judge_ios_packaging` (Staff iOS Native & Xcode/IPA Packaging Auditor, 100 pts)

| Criterion | Weight | Pass Criteria |
|---|---:|---|
| 1. Swift 5.9 / SwiftUI / WKWebView Correctness | 25 | Compiler-clean Swift 5.9 (`@MainActor`, thread-safe `WKURLSchemeHandler` guarding `stop(_:)`, SSE streaming proxy via `URLSessionDataDelegate`, iOS 15+ `WKUIDelegate` media permission grant). |
| 2. Offline WebBundle + Server Routing | 20 | `mindweave://app/` serves bundled `web/` cleanly + proxies `/v1/*` and `/healthz` to configurable server URL via native SwiftUI `ServerConfigSheet`. |
| 3. Native iOS Bridges & Info.plist | 20 | Real `UIImpactFeedbackGenerator` / `UISelectionFeedbackGenerator` / `UINotificationFeedbackGenerator`, `AVAudioSession`, `UIActivityViewController`, `UIPasteboard`, full privacy usage descriptions & launch screen. |
| 4. App Identity, AppIcon & Sync Script | 15 | High-res 1024×1024 Gleb-aesthetic `AppIcon.png` + valid Xcode 15+ `Contents.json` + working `scripts/sync_ios_web_bundle.sh`. |
| 5. macOS-15 Xcode 16 `.ipa` CI Workflow | 20 | `.github/workflows/build-ios-ipa.yml` builds real `arm64` Mach-O `MindWeave.ipa` via `xcodegen` + `xcodebuild`, supports unsigned sideload & optional signed distribution, verifies `Mach-O 64-bit executable arm64`, and uploads `.ipa` artifact. |
