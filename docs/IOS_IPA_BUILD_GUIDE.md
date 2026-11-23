# MindWeave (心络) — Native iOS App & `.ipa` Build Guide

This guide documents how to build, package, verify, and install the zero-dependency native SwiftUI + `WKWebView` iOS application (`MindWeave.ipa`, iOS 16.0–18+, `arm64`).

---

## 1. Architecture Overview

The iOS client lives under `ios/` and uses **zero third-party Swift/CocoaPods dependencies** (pure `SwiftUI`, `WebKit`, `UIKit`, `AVFoundation`, `Foundation`, Swift 5.9):

| Component | Path | Responsibility |
|---|---|---|
| App Entry & Deep Links | `ios/MindWeave/App/MindWeaveApp.swift` | `@main` SwiftUI lifecycle, edge-to-edge cotton-paper `#F7EFE3` canvas, thread-safe `ServerConfigStore`, `mindweave://server?url=...` deep-link handler |
| Custom Scheme & SSE Proxy | `ios/MindWeave/Bridge/MindWeaveSchemeHandler.swift` | `@MainActor` `WKURLSchemeHandler` serving bundled `WebBundle/` offline on `mindweave://app/` and transparently proxying `/v1/*` & `/healthz` (real-time SSE `/chat/stream`, `/chat`, `/events` + uncompressed `audio/*` / `206` `Content-Length` preservation) with `NSLock`-guarded cancellation safety |
| WebView Container | `ios/MindWeave/Bridge/WebContainerView.swift` | `UIViewRepresentable` `WKWebView`, retain-cycle-free `WKScriptMessageHandler` channels (`mwHaptic`, `mwShare`, `mwClipboard`, `mwAudio`, `mwConfig`), same-origin CORS sync (`window.__MW_API_BASE__ = ""` + live `window.__MW_SERVER_URL__`), iOS 15+ mic/camera permission grant, `tel:` hotline routing |
| Native Haptics, Audio & Share | `ios/MindWeave/Bridge/HapticsAndAudio.swift` | `UIImpactFeedbackGenerator` / `UISelectionFeedbackGenerator` / `UINotificationFeedbackGenerator`, `AVAudioSession` speaker+Bluetooth routing, iPad-safe `UIActivityViewController` exporting clean filenames from isolated temporary subdirectories |
| Server Config Sheet | `ios/MindWeave/Views/ServerConfigSheet.swift` | Gleb-warm SwiftUI sheet (44pt HIG targets) to edit backend URL, run live `/healthz` latency check, and reload cleanly |
| Launch & Assets | `ios/MindWeave/Resources/Assets.xcassets/` & `Info.plist` | Cotton-cream `#F7EFE3` `LaunchBackground.colorset` (zero cold-start white flash), opaque 1024×1024 RGB `AppIcon-1024.png`, `NSLocalNetworkUsageDescription`, mic/camera/photo usage strings |
| XcodeGen Project Spec | `ios/project.yml` | Single source of truth for generating `MindWeave.xcodeproj` with folder-referenced `MindWeave/WebBundle` |
| Web Bundle Sync | `scripts/sync_ios_web_bundle.sh` | Copies `web/` → `ios/MindWeave/WebBundle/` while stripping `tests/`, `*.test.mjs`, and `screenshots/` |
| CI `.ipa` Pipeline | `.github/workflows/build-ios-ipa.yml` | Automated `macos-15` + Xcode 16 build validating `project.yml`, opaque `AppIcon`, dual provisioning profile directories, `PlistBuddy` `ExportOptions.plist`, and verified `arm64` `MindWeave.ipa` artifacts |

---

## 2. One-Click GitHub Actions `.ipa` Build (Recommended)

You do **not** need a local Mac to generate a real `arm64` `MindWeave.ipa`.

1. Open your GitHub repository → **Actions** → **Build iOS IPA (MindWeave)**.
2. Click **Run workflow**.
3. Optionally enter:
   - **`api_base_url`**: Your deployed Cloud Run or LAN backend URL (baked into `Info.plist` as `MWDefaultAPIBaseURL`; defaults to `https://mindweave-backend.run.app`).
   - **`marketing_version`**: Optional version override (default `1.0.0`).
4. Wait ~3–5 minutes for the `macos-15` Xcode 16 runner to finish.
5. Download the **`MindWeave-iOS-IPA`** artifact containing `MindWeave.ipa`.

### Signed Distribution (Optional Secrets)

By default (with no secrets configured), the workflow builds a clean **unsigned Release `arm64` `.ipa`** ready for AltStore / Sideloadly / Xcode re-signing.

To produce a **signed Ad-Hoc or App Store / TestFlight `.ipa`** automatically in CI, add these GitHub Repository Secrets:

| Secret | Description |
|---|---|
| `IOS_P12_BASE64` | Base64-encoded Apple Distribution `.p12` certificate (`base64 -i cert.p12 \| pbcopy`) |
| `IOS_P12_PASSWORD` | Password used when exporting the `.p12` certificate |
| `IOS_PROVISION_PROFILE_BASE64` | Base64-encoded `.mobileprovision` profile matching bundle ID `app.mindweave.ios` (installed to both `~/Library/MobileDevice/Provisioning Profiles/` and Xcode 16's `~/Library/Developer/Xcode/UserData/Provisioning Profiles/`) |
| `IOS_EXPORT_METHOD` *(optional)* | Override export method (`ad-hoc`, `app-store-connect`, `enterprise`, `development`); auto-detected if omitted |

---

## 3. Building Locally on macOS (Xcode 16+)

### Prerequisites
- macOS 14+ with **Xcode 16+** installed
- Homebrew + [XcodeGen](https://github.com/yonaskolb/XcodeGen):
  ```bash
  brew install xcodegen
  ```

### Generate & Run in Xcode
```bash
# 1. Sync latest web/ frontend into ios/MindWeave/WebBundle/
./scripts/sync_ios_web_bundle.sh

# 2. Generate MindWeave.xcodeproj from ios/project.yml
cd ios && xcodegen generate

# 3. Open in Xcode (pick your signing team + iPhone device or Simulator, then Cmd+R)
open MindWeave.xcodeproj
```

### Build an Unsigned Release `arm64` `.ipa` from CLI
```bash
./scripts/sync_ios_web_bundle.sh
(cd ios && xcodegen generate)

xcodebuild \
  -project ios/MindWeave.xcodeproj \
  -scheme MindWeave \
  -configuration Release \
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  CONFIGURATION_BUILD_DIR="$PWD/ios/build/Release-iphoneos" \
  build

rm -rf ios/build/Payload ios/build/MindWeave.ipa
mkdir -p ios/build/Payload
cp -R ios/build/Release-iphoneos/MindWeave.app ios/build/Payload/MindWeave.app
(cd ios/build && zip -r -y MindWeave.ipa Payload)
```

### Verify the Built `.ipa`
```bash
unzip -q -o ios/build/MindWeave.ipa -d /tmp/mw_ipa_check
file /tmp/mw_ipa_check/Payload/MindWeave.app/MindWeave
# Expected: Mach-O 64-bit executable arm64

plutil -lint /tmp/mw_ipa_check/Payload/MindWeave.app/Info.plist
sips -g hasAlpha ios/MindWeave/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png
ls -la /tmp/mw_ipa_check/Payload/MindWeave.app/WebBundle/
unzip -l ios/build/MindWeave.ipa
```

---

## 4. Installing `MindWeave.ipa` on a Physical iPhone

- **Signed Ad-Hoc `.ipa`**: Connect iPhone via USB → open **Xcode → Window → Devices and Simulators** → drag `MindWeave.ipa` onto **Installed Apps** (or distribute via Apple Configurator / Diawi).
- **Unsigned `.ipa` (Personal Apple ID Sideload)**:
  - **Sideloadly** (macOS / Windows): Drag `MindWeave.ipa` into Sideloadly, sign with your Apple ID, and click **Start**.
  - **AltStore / SideStore**: Open `MindWeave.ipa` directly on iPhone inside AltStore to sign & install.
  - On first launch, enable **Settings → General → VPN & Device Management → Trust** for your Apple ID profile.

---

## 5. Configuring the Backend Server on iPhone

Because all UI assets load locally from `mindweave://app/` and `/v1/*` + `/healthz` proxy transparently through `MindWeaveSchemeHandler` (while `WebContainerView` clears any stale `mw.apiBase` and exposes the live configured URL on `window.__MW_SERVER_URL__`), you can point the installed iOS app at any backend without rebuilding:

1. **In-App Settings**: Open Settings → tap the server connection row (triggers `window.webkit.messageHandlers.mwConfig.postMessage({ action: "open" })`), test `/healthz`, and tap **保存并重载**.
2. **Any-Screen Shortcut**: Perform a **3-finger double-tap** anywhere in the app to open the native `ServerConfigSheet` (helpful on the login screen during Demo Day LAN switches).
3. **1-Tap Deep Link**: Open Safari or Notes on iPhone and tap:
   ```text
   mindweave://server?url=https://your-mindweave-backend.run.app
   ```
   or for local Mac LAN testing (matching `Makefile` `DEV_PORT=8000`):
   ```text
   mindweave://server?url=http://192.168.1.42:8000
   ```
