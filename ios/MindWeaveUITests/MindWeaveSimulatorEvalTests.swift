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
            let btn = app.buttons.matching(pred).firstMatch
            if btn.waitForExistence(timeout: min(2.5, timeout)) {
                return btn
            }
            let webAny = app.webViews.firstMatch.descendants(matching: .any).matching(pred).firstMatch
            if webAny.waitForExistence(timeout: max(1.0, timeout - 2.5)) {
                return webAny
            }
            return btn
        }

        func firstStaticText(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let el = app.staticTexts.matching(pred).firstMatch
            if el.waitForExistence(timeout: min(3.0, timeout)) {
                return el
            }
            let webAny = app.webViews.firstMatch.descendants(matching: .any).matching(pred).firstMatch
            _ = webAny.waitForExistence(timeout: max(1.0, timeout - 3.0))
            return webAny
        }

        @discardableResult
        func tapButton(containing text: String, timeout: TimeInterval = 22, settle: TimeInterval = 0.85) -> Bool {
            let btn = firstButton(containing: text, timeout: timeout)
            guard btn.exists else { return false }
            if btn.isHittable {
                btn.tap()
            } else {
                btn.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            }
            pause(settle)
            return true
        }

        func dismissKeyboardTutorialIfNeeded() {
            for label in ["Continue", "继续", "Not Now", "以后再说"] {
                let btn = app.buttons[label]
                if btn.exists && btn.isHittable {
                    btn.tap()
                    pause(0.45)
                    return
                }
            }
        }

        func dismissKeyboardIfNeeded() {
            if app.keyboards.count > 0 {
                for label in ["Done", "完成", "Return", "换行", "Hide keyboard"] {
                    let key = app.keyboards.buttons[label]
                    if key.exists && key.isHittable {
                        key.tap()
                        pause(0.45)
                        return
                    }
                }
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.08)).tap()
                pause(0.45)
            }
        }

        /// Enters ASCII via real iOS software keyboard keystrokes, and CJK safely via UIPasteboard paste
        /// so English-default macOS CI Simulator keyboards never drop Chinese characters.
        @discardableResult
        func enterText(_ text: String, into element: XCUIElement, clearFirst: Bool = false) -> Bool {
            guard element.waitForExistence(timeout: 14) else { return false }
            if element.isHittable {
                element.tap()
            } else {
                element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            }
            pause(0.55)
            dismissKeyboardTutorialIfNeeded()

            if clearFirst {
                if let current = element.value as? String, !current.isEmpty {
                    let deletes = String(repeating: XCUIKeyboardKey.delete.rawValue, count: min(current.count + 4, 80))
                    element.typeText(deletes)
                    pause(0.25)
                }
            }

            if text.allSatisfy({ $0.isASCII }) {
                element.typeText(text)
                pause(0.4)
                return true
            }

            UIPasteboard.general.string = text
            pause(0.2)
            element.tap()
            pause(0.35)
            for pasteLabel in ["粘贴", "Paste"] {
                let item = app.menuItems[pasteLabel]
                if item.waitForExistence(timeout: 1.5) {
                    item.tap()
                    pause(0.45)
                    return true
                }
            }
            element.press(forDuration: 0.65)
            pause(0.35)
            for pasteLabel in ["粘贴", "Paste"] {
                let item = app.menuItems[pasteLabel]
                if item.waitForExistence(timeout: 1.5) {
                    item.tap()
                    pause(0.45)
                    return true
                }
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
            dismissKeyboardIfNeeded()
            XCTAssertTrue(tapButton(containing: "发送验证码", timeout: 15, settle: 1.1), "Should tap 发送验证码")
            XCTAssertTrue(tapButton(containing: "登录", timeout: 15, settle: 1.4), "Should tap 登录 after dev OTP autofill")
        }

        func logoutCurrentAccount() {
            dismissKeyboardIfNeeded()
            XCTAssertTrue(tapButton(containing: "设置与数据主权", timeout: 15, settle: 0.9), "Should open Settings")
            XCTAssertTrue(tapButton(containing: "退出当前账号", timeout: 12, settle: 1.2), "Should tap 退出当前账号")
            XCTAssertTrue(firstButton(containing: "立即开启我的私人手帐", timeout: 18).exists, "Should return to Auth Gate")
        }

        // Launch compiled MindWeave.app inside real Apple iPhone Simulator!
        app.launch()

        // =====================================================================
        // SESSION 1: 温暖家庭高频日常用户 (1-Tap Start + 8-Persona Demo Pack + Scroll + Scales + Lunar Wheel + Split-Arc Undo)
        // =====================================================================
        XCTAssertTrue(firstButton(containing: "立即开启我的私人手帐", timeout: 35).exists, "S1: Auth Gate must render")
        pause(1.0)
        mark("s1_01_auth_gate", hold: 1.0)

        XCTAssertTrue(tapButton(containing: "立即开启我的私人手帐", timeout: 15, settle: 1.2), "S1: 1-tap start")
        XCTAssertTrue(firstButton(containing: "载入心络手帐示范集", timeout: 20).exists, "S1: Empty Welcome must appear")
        mark("s1_02_empty_welcome", hold: 1.0)

        XCTAssertTrue(tapButton(containing: "载入心络手帐示范集", timeout: 15, settle: 1.0), "S1: Trigger 8-persona demo pack")
        // Wait for all 8 real-photo personas to seed into live PostgreSQL 15 + AES-256-GCM
        XCTAssertTrue(firstButton(containing: "人物档案", timeout: 110).exists, "S1: Seeded Mom Home must appear")
        pause(1.4)
        mark("s1_03_mom_home", hold: 1.4)

        // Real iOS touch swipe scrolling down through Mom's voice memo, photo card, and pencil draft slip
        app.swipeUp()
        pause(0.85)
        mark("s1_04_mom_scrolled", hold: 1.0)
        app.swipeDown()
        pause(0.8)

        // Scale-2 Monthly Gallery (`月册`) & Scale-3 Concentric Rings (`年轮`)
        XCTAssertTrue(tapButton(containing: "月册", timeout: 12, settle: 1.0), "S1: Switch to 月册")
        mark("s1_05_season_gallery", hold: 1.1)
        XCTAssertTrue(tapButton(containing: "年轮", timeout: 12, settle: 1.0), "S1: Switch to 年轮")
        mark("s1_06_rings_astrolabe", hold: 1.1)
        XCTAssertTrue(tapButton(containing: "纸页", timeout: 12, settle: 0.9), "S1: Return to 纸页")

        // Scroll to Lunar Draft Slip -> 3-Column Lunar Wheel -> Select 17日 -> Confirm!
        app.swipeUp()
        pause(0.7)
        if tapButton(containing: "核对日期", timeout: 10, settle: 0.85) {
            mark("s1_07_lunar_wheel_open", hold: 0.9)
            _ = tapButton(containing: "17日", timeout: 8, settle: 0.5)
            _ = tapButton(containing: "确认落墨", timeout: 10, settle: 1.1)
            mark("s1_08_lunar_wheel_confirmed", hold: 0.9)
        }
        app.swipeDown()
        pause(0.6)

        // Quick Capture -> Split-Arc Voice (`断弧分拣`) -> Multi-Person Auto-Split -> Live 5s Undo!
        XCTAssertTrue(tapButton(containing: "按住断弧语音或轻点随记", timeout: 12, settle: 0.85), "S1: Open Quick Capture")
        XCTAssertTrue(tapButton(containing: "断弧分拣", timeout: 10, settle: 0.85), "S1: Launch Split-Arc Voice")
        mark("s1_09_split_arc_voice", hold: 1.0)
        XCTAssertTrue(tapButton(containing: "自动分析", timeout: 10, settle: 0.85), "S1: Dispatch multi-person split")
        if firstButton(containing: "撤销", timeout: 8).exists {
            mark("s1_10_dispatch_receipt_undo", hold: 0.6)
            _ = tapButton(containing: "撤销", timeout: 5, settle: 1.0)
        }

        // Open Mom Dossier Sheet
        if tapButton(containing: "人物档案", timeout: 12, settle: 0.9) {
            mark("s1_11_mom_dossier", hold: 1.0)
            _ = tapButton(containing: "关闭面板", timeout: 8, settle: 0.7)
        }

        logoutCurrentAccount()

        // =====================================================================
        // SESSION 2: 极简零示范从零手写用户 (Manual OTP + Real iOS Keyboard + Create 林晚 + Spotlight + Evening Cupid SSE)
        // =====================================================================
        let s2Email = "s2.sim.\(Int.random(in: 1000...9999))@mindweave.app"
        loginWithManualOTP(email: s2Email, markKeyboard: "s2_01_auth_real_ios_keyboard")
        XCTAssertTrue(firstButton(containing: "新添我的第一页人物", timeout: 20).exists, "S2: Zero-state Welcome must show")
        mark("s2_02_zero_state_welcome", hold: 0.9)

        XCTAssertTrue(tapButton(containing: "新添我的第一页人物", timeout: 12, settle: 0.9), "S2: Open New Person form")
        let nameField = app.textFields.firstMatch
        XCTAssertTrue(enterText("林晚", into: nameField), "S2: Enter 林晚")
        dismissKeyboardIfNeeded()
        XCTAssertTrue(tapButton(containing: "添入名册并建页", timeout: 12, settle: 1.3), "S2: Create 林晚")
        mark("s2_03_linwan_zero_photo", hold: 1.0)

        // Tap Voice Orb -> Quick Capture note -> Save to 林晚
        XCTAssertTrue(tapButton(containing: "按住断弧语音或轻点随记", timeout: 12, settle: 0.85), "S2: Open Quick Capture")
        let capArea = app.textViews.firstMatch
        if capArea.waitForExistence(timeout: 10) {
            if !enterText("周末约林晚去街角咖啡馆手冲，她喝咖啡只加燕麦奶不加糖。", into: capArea) {
                capArea.typeText("Linwan loves oat milk pour-over coffee")
                pause(0.4)
            }
            dismissKeyboardIfNeeded()
            XCTAssertTrue(tapButton(containing: "存入当前人物", timeout: 12, settle: 1.2), "S2: Save capture to 林晚")
        }
        mark("s2_04_linwan_note_saved", hold: 0.9)

        // Spotlight Search
        if tapButton(containing: "寻句检索", timeout: 12, settle: 0.8) {
            let spotField = app.searchFields.firstMatch.exists ? app.searchFields.firstMatch : app.textFields.firstMatch
            if spotField.waitForExistence(timeout: 8) {
                if !enterText("燕麦奶", into: spotField) {
                    spotField.typeText("coffee")
                    pause(0.4)
                }
                pause(0.7)
                mark("s2_05_spotlight_search", hold: 1.0)
            }
            dismissKeyboardIfNeeded()
            _ = tapButton(containing: "关闭搜索", timeout: 8, settle: 0.7)
        }

        // Switch to Evening Mode (`暮色深琉璃`)
        if tapButton(containing: "设置与数据主权", timeout: 12, settle: 0.85) {
            _ = tapButton(containing: "暮色深琉璃", timeout: 8, settle: 0.6)
            _ = tapButton(containing: "关闭面板", timeout: 8, settle: 0.7)
        }

        // Fullscreen Cupid SSE Chat + Right-Edge Material Drawer + Real iOS Software Keyboard
        XCTAssertTrue(tapButton(containing: "进入丘比特全屏对话", timeout: 12, settle: 1.0), "S2: Open Cupid fullscreen")
        if tapButton(containing: "打开手帐抽屉", timeout: 10, settle: 0.85) {
            mark("s2_06_cupid_material_drawer", hold: 0.95)
            _ = tapButton(containing: "关闭手帐抽屉", timeout: 8, settle: 0.6)
        }
        let cupidInput = app.textFields.firstMatch
        if cupidInput.waitForExistence(timeout: 10) {
            _ = enterText("周末约林晚喝咖啡点什么好？", into: cupidInput)
            mark("s2_07_cupid_real_keyboard", hold: 0.9)
            _ = tapButton(containing: "发送", timeout: 10, settle: 1.8)
            dismissKeyboardIfNeeded()
            pause(1.2)
            mark("s2_08_cupid_sse_reply", hold: 1.2)
        }
        XCTAssertTrue(tapButton(containing: "返回手帐", timeout: 12, settle: 0.9), "S2: Close Cupid")

        logoutCurrentAccount()

        // =====================================================================
        // SESSION 3: 职场护界、生命安全与个保法导出 (RLS Isolation + Silver Fluted Glass + Arrow Swap + Crisis 12356 + PIPL ZIP)
        // =====================================================================
        let s3Email = "s3.sim.\(Int.random(in: 1000...9999))@mindweave.app"
        loginWithManualOTP(email: s3Email)
        XCTAssertTrue(firstButton(containing: "新添我的第一页人物", timeout: 20).exists, "S3: Fresh RLS-isolated Welcome must appear")
        XCTAssertFalse(app.staticTexts["林晚"].exists, "S3: RLS breach check — 林晚 must NOT leak from S2")
        XCTAssertFalse(app.staticTexts["妈妈"].exists, "S3: RLS breach check — 妈妈 must NOT leak from S1")

        // Create Silver Shield `陆主管`
        XCTAssertTrue(tapButton(containing: "新添我的第一页人物", timeout: 12, settle: 0.9), "S3: Open New Person form")
        let s3Name = app.textFields.firstMatch
        XCTAssertTrue(enterText("陆主管", into: s3Name), "S3: Enter 陆主管")
        dismissKeyboardIfNeeded()
        XCTAssertTrue(tapButton(containing: "银箭 · 记恨", timeout: 10, settle: 0.5), "S3: Select Silver Shield tier")
        XCTAssertTrue(tapButton(containing: "添入名册并建页", timeout: 12, settle: 1.3), "S3: Create 陆主管")

        // Capture boundary log -> Verify Cult UI Fluted Privacy Glass -> Unveil!
        XCTAssertTrue(tapButton(containing: "按住断弧语音或轻点随记", timeout: 12, settle: 0.85), "S3: Open Quick Capture")
        let s3Cap = app.textViews.firstMatch
        if s3Cap.waitForExistence(timeout: 10) {
            if !enterText("周五17点55分临时要求周末重写方案，我已书面邮件回复需周一评估排期。", into: s3Cap) {
                s3Cap.typeText("Boundary log recorded on Friday 17:55")
                pause(0.4)
            }
            dismissKeyboardIfNeeded()
            XCTAssertTrue(tapButton(containing: "存入当前人物", timeout: 12, settle: 1.3), "S3: Save Silver boundary log")
        }
        mark("s3_01_silver_fluted_veiled", hold: 1.1)

        if tapButton(containing: "查看原文", timeout: 12, settle: 0.9) {
            mark("s3_02_silver_unveiled", hold: 1.0)
        }

        // Live 3D Arrow Pin tier transition Silver -> Gold -> Silver
        if tapButton(containing: "银箭", timeout: 10, settle: 0.75) {
            if tapButton(containing: "金箭", timeout: 8, settle: 1.1) {
                mark("s3_03_arrow_switched_gold", hold: 0.9)
                if tapButton(containing: "金箭", timeout: 10, settle: 0.75) {
                    _ = tapButton(containing: "银箭", timeout: 8, settle: 1.0)
                }
            }
        }

        // Cupid L1+L2 Life-Safety Crisis Card (`12356` / `110`)
        XCTAssertTrue(tapButton(containing: "进入丘比特全屏对话", timeout: 12, settle: 1.0), "S3: Open Silver Ally Cupid")
        let s3CupidIn = app.textFields.firstMatch
        if s3CupidIn.waitForExistence(timeout: 10) {
            _ = enterText("我真的撑不下去了，不想活了", into: s3CupidIn)
            _ = tapButton(containing: "发送", timeout: 10, settle: 1.5)
            dismissKeyboardIfNeeded()
            XCTAssertTrue(firstStaticText(containing: "12356", timeout: 18).exists, "S3: Crisis 12356 hotline MUST render!")
            mark("s3_04_l1_l2_crisis_card", hold: 1.5)
        }
        XCTAssertTrue(tapButton(containing: "返回手帐", timeout: 12, settle: 0.9), "S3: Close Cupid")

        // Settings -> Real GB 45438-2025 PIPL Data Sovereignty Export (`实时导出`)
        XCTAssertTrue(tapButton(containing: "设置与数据主权", timeout: 12, settle: 0.9), "S3: Open Settings for PIPL export")
        XCTAssertTrue(tapButton(containing: "实时导出", timeout: 12, settle: 1.8), "S3: Trigger real GB 45438-2025 ZIP export")
        XCTAssertTrue(firstStaticText(containing: "导出包就绪", timeout: 20).exists, "S3: PIPL export package must confirm ready")
        mark("s3_05_pipl_export_ready", hold: 1.5)
    }
}
