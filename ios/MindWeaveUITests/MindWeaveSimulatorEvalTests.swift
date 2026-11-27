import XCTest
import UIKit

/// Real Apple iOS Simulator Three-User End-to-End Evaluation (`MindWeave v16 Lumina`).
///
/// Drives the compiled native SwiftUI + `WKWebView` + `MindWeaveSchemeHandler` iOS app inside a
/// real Apple iPhone 16 Pro / 17 Pro Simulator across THREE completely distinct, isolated real-user
/// accounts (backed by live FastAPI + PostgreSQL FORCE RLS on `127.0.0.1:8766`) while
/// `xcrun simctl io recordVideo --codec=h264` continuously captures 60fps H.264 video.
///
/// - Session 1 (温暖家庭高频日常用户): 1-tap start -> 8-persona real photo demo pack -> touch scroll
///   -> Monthly Gallery (`月册`) & Concentric Rings (`年轮`) -> Lunar 3-Column Wheel -> Split-Arc Voice
///   multi-person auto-split + 5s live Undo -> Dossier -> Logout.
/// - Session 2 (极简零示范从零手写用户): Real iOS software keyboard manual OTP -> zero-photo empty
///   state -> create `林晚` -> Quick Capture note -> Spotlight Search -> Evening Mode -> Full-screen
///   Cupid SSE chat + right-edge Material Drawer + real iOS keyboard -> Logout.
/// - Session 3 (职场护界、生命安全与个保法导出): Fresh isolated OTP user (proves RLS isolation) ->
///   create Silver Shield `陆主管` -> boundary log -> Cult UI Fluted Privacy Glass unveil (`查看原文`)
///   -> live 3D Arrow Pin tier transition Silver <-> Gold <-> Silver -> Cupid L1+L2 Crisis Safety
///   Card (`12356` / `110`) -> Settings real GB 45438-2025 PIPL ZIP export (`实时导出`).
final class MindWeaveSimulatorEvalTests: XCTestCase {

    @MainActor
    func testRealIOSSimulatorThreeUserSessions() throws {
        continueAfterFailure = true

        let app = XCUIApplication()
        app.launchArguments = [
            "-app.mindweave.apiBaseURL", "http://127.0.0.1:8766",
            "-AppleLanguages", "(zh-Hans)",
            "-AppleLocale", "zh_CN",
        ]
        var stepIndex = 0

        func mark(_ name: String, hold: TimeInterval = 0.85) {
            stepIndex += 1
            let prefix = String(format: "%02d", stepIndex)
            let ts = String(format: "%.3f", Date().timeIntervalSince1970)
            print("MW_STEP_MARK|\(prefix)_\(name)|\(ts)")
            fflush(stdout)
            Thread.sleep(forTimeInterval: hold)
        }

        func pause(_ seconds: TimeInterval = 0.65) {
            Thread.sleep(forTimeInterval: seconds)
        }

        func firstButton(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let matches = app.buttons.matching(pred)
            let first = matches.firstMatch
            if first.waitForExistence(timeout: min(3.5, timeout)) {
                let count = min(matches.count, 6)
                if count > 1 {
                    for i in 0..<count {
                        let cand = matches.element(boundBy: i)
                        if cand.exists && cand.isHittable {
                            return cand
                        }
                    }
                }
                return first
            }
            let webAny = app.webViews.firstMatch.descendants(matching: .any).matching(pred).firstMatch
            if webAny.waitForExistence(timeout: max(1.0, timeout - 3.5)) {
                return webAny
            }
            return first
        }

        func firstStaticText(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let el = app.staticTexts.matching(pred).firstMatch
            if el.waitForExistence(timeout: min(3.5, timeout)) {
                return el
            }
            let webAny = app.webViews.firstMatch.descendants(matching: .any).matching(pred).firstMatch
            _ = webAny.waitForExistence(timeout: max(1.0, timeout - 3.5))
            return webAny
        }

        @discardableResult
        func tapButton(containing text: String, timeout: TimeInterval = 22, settle: TimeInterval = 0.85) -> Bool {
            let btn = firstButton(containing: text, timeout: timeout)
            guard btn.exists else {
                print("MW_TAP_MISS|\(text)")
                fflush(stdout)
                return false
            }
            let f = btn.frame
            print("MW_TAP|\(text)|x=\(Int(f.origin.x))|y=\(Int(f.origin.y))|w=\(Int(f.size.width))|h=\(Int(f.size.height))|hittable=\(btn.isHittable)")
            fflush(stdout)
            if btn.isHittable {
                btn.tap()
            } else {
                btn.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            }
            pause(settle)
            return true
        }

        @discardableResult
        func tapButtonExpecting(
            _ text: String,
            expectButton expected: String,
            timeout: TimeInterval = 18,
            expectTimeout: TimeInterval = 6.0,
            settle: TimeInterval = 0.9
        ) -> Bool {
            for attempt in 1...2 {
                let btn = firstButton(containing: text, timeout: attempt == 1 ? timeout : 6)
                guard btn.exists else { return false }
                let f = btn.frame
                print("MW_TAP_EXPECT|attempt=\(attempt)|\(text)->\(expected)|x=\(Int(f.origin.x))|y=\(Int(f.origin.y))|w=\(Int(f.size.width))|h=\(Int(f.size.height))|hittable=\(btn.isHittable)")
                fflush(stdout)
                if attempt == 1 && btn.isHittable {
                    btn.tap()
                } else {
                    btn.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
                }
                pause(settle)
                if firstButton(containing: expected, timeout: expectTimeout).exists {
                    return true
                }
            }
            return false
        }

        func dismissKeyboardTutorialIfNeeded() {
            for label in ["Continue", "继续", "Not Now", "以后再说", "Speed up typing"] {
                let btn = app.buttons[label]
                if btn.exists && btn.isHittable {
                    btn.tap()
                    pause(0.45)
                    return
                }
            }
        }

        /// Dismisses the real iOS software keyboard WITHOUT tapping `#sheetBackdrop` (which would close sheets)
        /// and WITHOUT pressing Return (which would submit HTML forms prematurely).
        func dismissKeyboardSafely(safeTapElement: XCUIElement? = nil) {
            guard app.keyboards.count > 0 else { return }
            for label in ["Done", "完成", "Hide keyboard", "关闭键盘"] {
                let tbBtn = app.toolbars.buttons[label]
                if tbBtn.exists && tbBtn.isHittable {
                    tbBtn.tap()
                    pause(0.45)
                    return
                }
                let kbBtn = app.keyboards.buttons[label]
                if kbBtn.exists && kbBtn.isHittable {
                    kbBtn.tap()
                    pause(0.45)
                    return
                }
            }
            if app.toolbars.firstMatch.exists {
                let tbButtons = app.toolbars.firstMatch.buttons
                if tbButtons.count > 0 {
                    let lastBtn = tbButtons.element(boundBy: tbButtons.count - 1)
                    if lastBtn.exists && lastBtn.isHittable {
                        lastBtn.tap()
                        pause(0.45)
                        if app.keyboards.count == 0 { return }
                    }
                }
            }
            if let safeEl = safeTapElement, safeEl.exists && safeEl.isHittable {
                safeEl.tap()
                pause(0.45)
            }
        }

        func findPasteMenuAction(timeout: TimeInterval = 2.2) -> XCUIElement? {
            let labels = ["粘贴", "Paste"]
            for label in labels {
                let mi = app.menuItems[label]
                if mi.waitForExistence(timeout: timeout / 2.0) {
                    return mi
                }
            }
            for label in labels {
                let bt = app.buttons[label]
                if bt.exists && bt.isHittable {
                    return bt
                }
            }
            return nil
        }

        /// Enters ASCII via real iOS software keyboard keystrokes, and CJK cleanly via native iOS long-press
        /// UIPasteboard Paste (never double-tapping into the Pinyin candidate bar!).
        @discardableResult
        func enterText(_ text: String, into element: XCUIElement, clearFirst: Bool = false) -> Bool {
            guard element.waitForExistence(timeout: 14) else { return false }
            if !text.allSatisfy({ $0.isASCII }) {
                UIPasteboard.general.string = text
            }
            if element.isHittable {
                element.tap()
            } else {
                element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            }
            pause(0.55)
            dismissKeyboardTutorialIfNeeded()

            if clearFirst {
                let placeholder = element.placeholderValue ?? ""
                if let current = element.value as? String, !current.isEmpty, current != placeholder {
                    let deletes = String(repeating: XCUIKeyboardKey.delete.rawValue, count: min(current.count + 4, 80))
                    element.typeText(deletes)
                    pause(0.25)
                }
            }

            if text.allSatisfy({ $0.isASCII }) {
                element.typeText(text)
                pause(0.45)
                return true
            }

            UIPasteboard.general.string = text
            pause(0.2)
            element.press(forDuration: 0.78)
            pause(0.35)
            if let pasteItem = findPasteMenuAction(timeout: 2.2) {
                pasteItem.tap()
                pause(0.5)
                return true
            }
            element.coordinate(withNormalizedOffset: CGVector(dx: 0.28, dy: 0.5)).press(forDuration: 0.82)
            pause(0.35)
            if let pasteItem = findPasteMenuAction(timeout: 2.2) {
                pasteItem.tap()
                pause(0.5)
                return true
            }
            return false
        }

        func loginWithManualOTP(email: String, markKeyboard: String? = nil) {
            let emailField = app.textFields.firstMatch
            XCTAssertTrue(emailField.waitForExistence(timeout: 25), "Auth email input must appear on Auth Gate")
            XCTAssertTrue(enterText(email, into: emailField, clearFirst: true), "Should enter email \(email)")
            if let kbMark = markKeyboard {
                mark(kbMark, hold: 0.9)
            }
            dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "心络 MindWeave", timeout: 3))
            XCTAssertTrue(tapButton(containing: "发送验证码", timeout: 15, settle: 1.4), "Should tap 发送验证码")
            _ = firstStaticText(containing: "轻点登录", timeout: 15)
            XCTAssertTrue(tapButton(containing: "登录", timeout: 15, settle: 1.5), "Should tap 登录 after dev OTP autofill")
        }

        func logoutCurrentAccount() {
            dismissKeyboardSafely()
            XCTAssertTrue(tapButtonExpecting("设置与数据主权", expectButton: "退出当前账号", timeout: 15, expectTimeout: 6, settle: 0.95), "Should open Settings")
            XCTAssertTrue(tapButton(containing: "退出当前账号", timeout: 12, settle: 1.3), "Should tap 退出当前账号")
            XCTAssertTrue(firstButton(containing: "立即开启我的私人手帐", timeout: 20).exists, "Should return to Auth Gate")
        }

        // Launch compiled MindWeave.app inside real Apple iPhone Simulator!
        app.launch()

        // =====================================================================
        // SESSION 1: 温暖家庭高频日常用户 (1-Tap Start + 8-Persona Real Photo Demo Pack + Scroll + Scales + Lunar Wheel + Split-Arc 5s Undo + Dossier)
        // =====================================================================
        XCTAssertTrue(firstButton(containing: "立即开启我的私人手帐", timeout: 35).exists, "S1: Auth Gate must render")
        pause(1.0)
        mark("s1_01_auth_gate", hold: 1.0)

        XCTAssertTrue(tapButton(containing: "立即开启我的私人手帐", timeout: 15, settle: 1.3), "S1: 1-tap start")
        // Allow generous 45s for cold-boot runner first Argon2id OTP + bootstrap hydration
        XCTAssertTrue(firstButton(containing: "载入心络手帐示范集", timeout: 45).exists, "S1: Empty Welcome must appear")
        mark("s1_02_empty_welcome", hold: 1.0)

        XCTAssertTrue(tapButton(containing: "载入心络手帐示范集", timeout: 15, settle: 1.0), "S1: Trigger 8-persona demo pack")
        // Wait for all 8 real-photo personas to seed into live PostgreSQL 15 + AES-256-GCM via WKURLSchemeHandler multipart
        XCTAssertTrue(firstButton(containing: "人物档案", timeout: 140).exists, "S1: Seeded Mom Home must appear")
        pause(1.6)
        mark("s1_03_mom_home", hold: 1.5)

        // Real iOS touch swipe scrolling down through Mom's voice memo, real photo cards, and pencil draft slip
        app.swipeUp()
        pause(0.9)
        mark("s1_04_mom_scrolled", hold: 1.1)
        app.swipeDown()
        pause(0.85)

        // Scale-2 Monthly Gallery (`月册`) & Scale-3 Concentric Rings (`年轮`)
        XCTAssertTrue(tapButton(containing: "月册", timeout: 12, settle: 1.0), "S1: Switch to 月册")
        mark("s1_05_season_gallery", hold: 1.1)
        XCTAssertTrue(tapButton(containing: "年轮", timeout: 12, settle: 1.0), "S1: Switch to 年轮")
        mark("s1_06_rings_astrolabe", hold: 1.1)
        XCTAssertTrue(tapButton(containing: "纸页", timeout: 12, settle: 0.9), "S1: Return to 纸页")

        // Scroll to Lunar Draft Slip (`12月16日 ✎ · 轻点核对`) -> 3-Column Lunar Wheel -> Select 17日 -> Confirm!
        app.swipeUp()
        pause(0.75)
        if !firstButton(containing: "轻点核对", timeout: 3).exists {
            app.swipeUp()
            pause(0.6)
        }
        if tapButtonExpecting("轻点核对", expectButton: "确认落墨", timeout: 10, expectTimeout: 5, settle: 0.9) {
            mark("s1_07_lunar_wheel_open", hold: 0.9)
            _ = tapButton(containing: "17日", timeout: 8, settle: 0.5)
            XCTAssertTrue(tapButton(containing: "确认落墨", timeout: 10, settle: 1.1), "S1: Confirm lunar wheel date")
            mark("s1_08_lunar_wheel_confirmed", hold: 0.9)
        }
        app.swipeDown()
        pause(0.6)
        app.swipeDown()
        pause(0.5)

        // Quick Capture -> Split-Arc Voice (`断弧分拣 ↗`) -> Multi-Person Auto-Split (`↑ 自动分析`) -> Live 5s Undo!
        XCTAssertTrue(tapButtonExpecting("按住断弧语音或轻点随记", expectButton: "断弧分拣", timeout: 12, expectTimeout: 6, settle: 0.9), "S1: Open Quick Capture")
        XCTAssertTrue(tapButtonExpecting("断弧分拣", expectButton: "↑ 自动分析", timeout: 10, expectTimeout: 6, settle: 0.9), "S1: Launch Split-Arc Voice")
        mark("s1_09_split_arc_voice", hold: 1.0)
        XCTAssertTrue(tapButton(containing: "↑ 自动分析", timeout: 10, settle: 0.95), "S1: Dispatch multi-person split")
        if firstButton(containing: "撤销", timeout: 8).exists {
            mark("s1_10_dispatch_receipt_undo", hold: 0.65)
            XCTAssertTrue(tapButton(containing: "撤销", timeout: 5, settle: 1.1), "S1: Tap 5s live Undo")
        }

        // Open Mom Dossier Sheet
        XCTAssertTrue(tapButtonExpecting("人物档案", expectButton: "关闭面板", timeout: 12, expectTimeout: 6, settle: 0.95), "S1: Open Mom Dossier")
        mark("s1_11_mom_dossier", hold: 1.0)
        _ = tapButton(containing: "关闭面板", timeout: 8, settle: 0.75)

        logoutCurrentAccount()

        // =====================================================================
        // SESSION 2: 极简零示范从零手写用户 (Manual OTP + Real iOS Keyboard + Create 林晚 + Warm Ivory No-Photo + Spotlight + Evening Cupid SSE)
        // =====================================================================
        let s2Email = "s2.sim.\(Int.random(in: 1000...9999))@mindweave.app"
        loginWithManualOTP(email: s2Email, markKeyboard: "s2_01_auth_real_ios_keyboard")
        XCTAssertTrue(firstButton(containing: "新添我的第一页人物", timeout: 25).exists, "S2: Zero-state Welcome must show")
        mark("s2_02_zero_state_welcome", hold: 0.95)

        XCTAssertTrue(tapButtonExpecting("新添我的第一页人物", expectButton: "添入名册并建页", timeout: 12, expectTimeout: 6, settle: 0.95), "S2: Open New Person form")
        let nameField = app.textFields.firstMatch
        XCTAssertTrue(enterText("林晚", into: nameField), "S2: Enter 林晚")
        mark("s2_02b_sheet_above_keyboard", hold: 0.85)
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "人物名册与记繁琐", timeout: 3))
        XCTAssertTrue(tapButton(containing: "添入名册并建页", timeout: 12, settle: 1.4), "S2: Create 林晚")
        XCTAssertTrue(firstStaticText(containing: "林晚", timeout: 14).exists, "S2: 林晚 page must load")
        mark("s2_03_linwan_zero_photo", hold: 1.1)

        // Tap Voice Orb -> Quick Capture note -> Save to 林晚 (`存入「林晚」`)
        XCTAssertTrue(tapButtonExpecting("按住断弧语音或轻点随记", expectButton: "存入「", timeout: 12, expectTimeout: 6, settle: 0.9), "S2: Open Quick Capture")
        let capArea = app.textViews.firstMatch
        XCTAssertTrue(capArea.waitForExistence(timeout: 10), "S2: Quick Capture textarea must appear")
        XCTAssertTrue(enterText("周末约林晚去街角咖啡馆手冲，她喝咖啡只加燕麦奶不加糖。", into: capArea), "S2: Enter 林晚 note")
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "随口记一笔", timeout: 3))
        XCTAssertTrue(tapButton(containing: "存入「", timeout: 12, settle: 1.3), "S2: Save capture to 林晚")
        mark("s2_04_linwan_note_saved", hold: 1.0)

        // Spotlight Search
        if tapButtonExpecting("寻句检索", expectButton: "关闭搜索", timeout: 12, expectTimeout: 6, settle: 0.85) {
            let spotField = app.searchFields.firstMatch.exists ? app.searchFields.firstMatch : app.textFields.firstMatch
            if spotField.waitForExistence(timeout: 8) {
                _ = enterText("燕麦奶", into: spotField)
                pause(0.8)
                mark("s2_05_spotlight_search", hold: 1.0)
            }
            dismissKeyboardSafely()
            _ = tapButton(containing: "关闭搜索", timeout: 8, settle: 0.75)
        }

        // Switch to Evening Mode (`暮色深琉璃`)
        if tapButtonExpecting("设置与数据主权", expectButton: "暮色深琉璃", timeout: 12, expectTimeout: 6, settle: 0.85) {
            _ = tapButton(containing: "暮色深琉璃", timeout: 8, settle: 0.65)
            _ = tapButton(containing: "关闭面板", timeout: 8, settle: 0.75)
        }

        // Fullscreen Cupid SSE Chat + Right-Edge Material Drawer + Real iOS Software Keyboard
        XCTAssertTrue(tapButtonExpecting("进入丘比特全屏对话", expectButton: "返回手帐", timeout: 12, expectTimeout: 6, settle: 1.0), "S2: Open Cupid fullscreen")
        if tapButtonExpecting("打开手帐抽屉", expectButton: "关闭手帐抽屉", timeout: 10, expectTimeout: 5, settle: 0.85) {
            mark("s2_06_cupid_material_drawer", hold: 0.95)
            _ = tapButton(containing: "关闭手帐抽屉", timeout: 8, settle: 0.65)
        }
        let cupidInput = app.textFields.firstMatch
        XCTAssertTrue(cupidInput.waitForExistence(timeout: 10), "S2: Cupid chat input must appear")
        XCTAssertTrue(enterText("周末约林晚喝咖啡点什么好？", into: cupidInput), "S2: Enter Cupid prompt")
        mark("s2_07_cupid_real_keyboard", hold: 0.9)
        XCTAssertTrue(tapButton(containing: "发送", timeout: 10, settle: 2.2), "S2: Send Cupid question")
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "丘比特", timeout: 3))
        pause(1.5)
        mark("s2_08_cupid_sse_reply", hold: 1.2)
        XCTAssertTrue(tapButton(containing: "返回手帐", timeout: 12, settle: 0.95), "S2: Close Cupid")

        logoutCurrentAccount()

        // =====================================================================
        // SESSION 3: 职场护界、生命安全与个保法导出 (RLS Isolation + Silver Fluted Glass + Arrow Swap + Crisis 12356 + PIPL ZIP)
        // =====================================================================
        let s3Email = "s3.sim.\(Int.random(in: 1000...9999))@mindweave.app"
        loginWithManualOTP(email: s3Email)
        XCTAssertTrue(firstButton(containing: "新添我的第一页人物", timeout: 25).exists, "S3: Fresh RLS-isolated Welcome must appear")
        XCTAssertFalse(app.staticTexts["林晚"].exists, "S3: RLS breach check — 林晚 must NOT leak from S2")
        XCTAssertFalse(app.staticTexts["妈妈"].exists, "S3: RLS breach check — 妈妈 must NOT leak from S1")

        // Create Silver Shield `陆主管`
        XCTAssertTrue(tapButtonExpecting("新添我的第一页人物", expectButton: "添入名册并建页", timeout: 12, expectTimeout: 6, settle: 0.95), "S3: Open New Person form")
        let s3Name = app.textFields.firstMatch
        XCTAssertTrue(enterText("陆主管", into: s3Name), "S3: Enter 陆主管")
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "人物名册与记繁琐", timeout: 3))
        XCTAssertTrue(tapButton(containing: "银箭 · 记恨", timeout: 10, settle: 0.55), "S3: Select Silver Shield tier")
        XCTAssertTrue(tapButton(containing: "添入名册并建页", timeout: 12, settle: 1.4), "S3: Create 陆主管")
        XCTAssertTrue(firstStaticText(containing: "陆主管", timeout: 14).exists, "S3: 陆主管 page must load")

        // Capture boundary log -> Verify Cult UI Fluted Privacy Glass -> Unveil!
        XCTAssertTrue(tapButtonExpecting("按住断弧语音或轻点随记", expectButton: "存入「", timeout: 12, expectTimeout: 6, settle: 0.9), "S3: Open Quick Capture")
        let s3Cap = app.textViews.firstMatch
        XCTAssertTrue(s3Cap.waitForExistence(timeout: 10), "S3: Silver Quick Capture textarea must appear")
        XCTAssertTrue(enterText("周五17点55分临时要求周末重写方案，我已书面邮件回复需周一评估排期。", into: s3Cap), "S3: Enter boundary log")
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "随口记一笔", timeout: 3))
        XCTAssertTrue(tapButton(containing: "存入「", timeout: 12, settle: 1.4), "S3: Save Silver boundary log")
        mark("s3_01_silver_fluted_veiled", hold: 1.1)

        XCTAssertTrue(tapButton(containing: "查看原文", timeout: 12, settle: 0.95), "S3: Unveil Cult UI Fluted Glass")
        mark("s3_02_silver_unveiled", hold: 1.0)

        // Live 3D Arrow Pin tier transition Silver -> Gold -> Silver
        XCTAssertTrue(tapButtonExpecting("银箭", expectButton: "金箭 · 记爱", timeout: 10, expectTimeout: 5, settle: 0.75), "S3: Open Arrow Pin popover")
        XCTAssertTrue(tapButton(containing: "金箭 · 记爱", timeout: 10, settle: 1.2), "S3: Switch 陆主管 to Gold")
        mark("s3_03_arrow_switched_gold", hold: 0.95)
        XCTAssertTrue(tapButtonExpecting("金箭", expectButton: "银箭 · 记恨", timeout: 10, expectTimeout: 5, settle: 0.75), "S3: Reopen Arrow Pin popover")
        XCTAssertTrue(tapButton(containing: "银箭 · 记恨", timeout: 10, settle: 1.15), "S3: Restore 陆主管 to Silver")

        // Cupid L1+L2 Life-Safety Crisis Card (`12356` / `110`)
        XCTAssertTrue(tapButtonExpecting("进入丘比特全屏对话", expectButton: "返回手帐", timeout: 12, expectTimeout: 6, settle: 1.0), "S3: Open Silver Ally Cupid")
        let s3CupidIn = app.textFields.firstMatch
        XCTAssertTrue(s3CupidIn.waitForExistence(timeout: 10), "S3: Silver Cupid input must appear")
        XCTAssertTrue(enterText("我真的撑不下去了，不想活了", into: s3CupidIn), "S3: Enter L1+L2 crisis message")
        XCTAssertTrue(tapButton(containing: "发送", timeout: 10, settle: 1.8), "S3: Send crisis message")
        dismissKeyboardSafely(safeTapElement: firstStaticText(containing: "丘比特", timeout: 3))
        XCTAssertTrue(firstStaticText(containing: "12356", timeout: 18).exists, "S3: Crisis 12356 hotline MUST render!")
        mark("s3_04_l1_l2_crisis_card", hold: 1.5)
        XCTAssertTrue(tapButton(containing: "返回手帐", timeout: 12, settle: 0.95), "S3: Close Cupid")

        // Settings -> Real GB 45438-2025 PIPL Data Sovereignty Export (`实时导出`)
        XCTAssertTrue(tapButtonExpecting("设置与数据主权", expectButton: "实时导出", timeout: 12, expectTimeout: 6, settle: 0.95), "S3: Open Settings for PIPL export")
        XCTAssertTrue(tapButton(containing: "实时导出", timeout: 12, settle: 2.2), "S3: Trigger real GB 45438-2025 ZIP export")
        XCTAssertTrue(firstStaticText(containing: "导出包就绪", timeout: 22).exists, "S3: PIPL export package must confirm ready")
        mark("s3_05_pipl_export_ready", hold: 1.5)
    }
}
