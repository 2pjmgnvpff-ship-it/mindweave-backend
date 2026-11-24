import UIKit
import XCTest

/// Real Apple Xcode iOS Simulator end-to-end hands-on user evaluation for MindWeave (心络).
/// Drives the compiled `MindWeave.app` (`WKWebView` + `MindWeaveSchemeHandler` + Swift bridges)
/// inside a real Apple `iPhone 16 Pro` Simulator using genuine touch taps, real iOS software
/// keyboard input, scrolling, stacked sheets, and multi-atmosphere switching while `xcrun simctl`
/// continuously records full-motion H.264 video (`recordVideo --codec=h264`) with zero IOSurface
/// screenshot lock contention.
final class MindWeaveSimulatorEvalTests: XCTestCase {

    @MainActor
    func testFullRealIOSSimulatorWalkthrough() throws {
        continueAfterFailure = true

        let app = XCUIApplication()
        app.launchArguments = [
            "-app.mindweave.apiBaseURL", "http://127.0.0.1:8766",
            "-AppleLanguages", "(zh-Hans)",
            "-AppleLocale", "zh_CN",
        ]
        var stepIndex = 0

        /// Marks a clean visual beat in stdout (paired with `simctl io recordVideo` UTC epoch)
        /// without invoking `XCUIScreen.main.screenshot()` which contends for IOSurface on headless CI.
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

        /// Matches both standard `<button>` and ARIA `<button role="radio|tab|menuitem">` inside iOS WKWebView.
        func firstButton(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let btn = app.buttons.matching(pred).firstMatch
            if btn.waitForExistence(timeout: min(2.0, timeout)) {
                return btn
            }
            let webAny = app.webViews.firstMatch.descendants(matching: .any).matching(pred).firstMatch
            if webAny.waitForExistence(timeout: max(1.0, timeout - 2.0)) {
                return webAny
            }
            return btn
        }

        func firstStaticText(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let el = app.staticTexts.matching(pred).firstMatch
            _ = el.waitForExistence(timeout: timeout)
            return el
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

        func tapBackdropTop(settle: TimeInterval = 0.8) {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.085)).tap()
            pause(settle)
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
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)).tap()
                pause(0.45)
            }
        }

        func dismissKeyboardTutorialIfNeeded() {
            for label in ["Continue", "继续", "Not Now", "以后再说"] {
                let btn = app.buttons[label]
                if btn.exists && btn.isHittable {
                    btn.tap()
                    pause(0.55)
                    return
                }
            }
        }

        /// Enters ASCII via real iOS software keyboard keystrokes, and CJK safely via UIPasteboard paste
        /// so English-default iOS Simulator keyboards never fail trying to synthesize CJK keycodes.
        @discardableResult
        func enterText(_ text: String, into element: XCUIElement) -> Bool {
            guard element.waitForExistence(timeout: 12) else { return false }
            element.tap()
            pause(0.5)
            dismissKeyboardTutorialIfNeeded()
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
                if item.waitForExistence(timeout: 1.4) {
                    item.tap()
                    pause(0.45)
                    return true
                }
            }
            element.press(forDuration: 0.65)
            pause(0.35)
            for pasteLabel in ["粘贴", "Paste"] {
                let item = app.menuItems[pasteLabel]
                if item.waitForExistence(timeout: 1.4) {
                    item.tap()
                    pause(0.45)
                    return true
                }
            }
            return false
        }

        func loginWithEmail(_ email: String, markSplash: Bool) {
            let emailField = app.textFields.firstMatch
            XCTAssertTrue(emailField.waitForExistence(timeout: 30), "Auth email input should appear on splash")
            pause(1.4)
            if markSplash {
                mark("sim_splash_lakeside", hold: 1.0)
            }

            // Tap right edge so caret always lands at the end of any remembered previous email
            emailField.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
            pause(0.85)
            dismissKeyboardTutorialIfNeeded()
            pause(0.45)
            if markSplash {
                mark("sim_splash_ios_keyboard", hold: 0.9)
            }

            if let currentVal = emailField.value as? String, !currentVal.isEmpty, currentVal != "输入你的邮箱" {
                let deleteString = String(repeating: XCUIKeyboardKey.delete.rawValue, count: currentVal.count + 8)
                emailField.typeText(deleteString)
                pause(0.3)
            }
            emailField.typeText(email)
            pause(0.45)

            XCTAssertTrue(tapButton(containing: "获取验证码", timeout: 15, settle: 0.9), "Should tap 获取验证码")
            let verifyBtn = firstButton(containing: "翻开手帐", timeout: 30)
            XCTAssertTrue(verifyBtn.exists, "OTP step 翻开手帐 button should appear")
            pause(0.4)
            if markSplash {
                mark("sim_otp_step", hold: 0.9)
            }

            verifyBtn.tap()
            pause(1.2)
        }

        // Launch compiled MindWeave.app inside real Apple iPhone Simulator!
        app.launch()

        // =====================================================================
        // PART 1: COLD-START BRAND-NEW USER (Splash -> Real iOS Keyboard -> OTP -> 3-Step Onboarding)
        // =====================================================================
        loginWithEmail("newuser_sim@example.com", markSplash: true)

        let step1Heading = firstStaticText(containing: "把重要的人", timeout: 30)
        XCTAssertTrue(step1Heading.exists, "New user should land on Onboarding Step 1")
        pause(0.5)
        mark("sim_onb_step1", hold: 0.9)

        let exampleChip = firstButton(containing: "妈妈", timeout: 15)
        if exampleChip.exists {
            exampleChip.tap()
            pause(0.6)
        }
        XCTAssertTrue(tapButton(containing: "记下这句", timeout: 15, settle: 0.8), "Should submit Step 1 sentence")

        let goldArrowBtn = firstButton(containing: "金箭", timeout: 35)
        XCTAssertTrue(goldArrowBtn.exists, "Onboarding Step 2 Gold arrow card should appear")
        pause(0.5)
        mark("sim_onb_step2", hold: 0.9)

        goldArrowBtn.tap()
        pause(1.0)

        let enterJournalBtn = firstButton(containing: "翻开", timeout: 35)
        XCTAssertTrue(enterJournalBtn.exists, "Onboarding Step 3 enter journal button should appear")
        pause(0.6)
        mark("sim_onb_step3_cupid", hold: 1.0)

        enterJournalBtn.tap()
        _ = firstButton(containing: "设置", timeout: 30)
        pause(1.0)
        mark("sim_newuser_journal_home", hold: 0.9)

        // Sign out via Settings -> switch to rich multi-year seeded demo user
        XCTAssertTrue(tapButton(containing: "设置", timeout: 15, settle: 0.9), "Should open Settings")
        XCTAssertTrue(tapButton(containing: "退出登录", timeout: 15, settle: 1.4), "Should sign out to splash")

        // =====================================================================
        // PART 2: RICH MULTI-YEAR DEMO USER DAILY LIFE (7 Personas, Gold/Silver/Crystal, Photos, Reminders, Pencil Drafts)
        // =====================================================================
        loginWithEmail("demo@mindweave.app", markSplash: false)

        let pocketBtn = firstButton(containing: "人物档案", timeout: 35)
        XCTAssertTrue(pocketBtn.exists, "Demo user journal home should load with 人物档案 link")
        pause(1.2)
        mark("sim_demo_journal_home", hold: 1.2)

        // Real finger scroll down through photo slip, voice memo, pencil draft, then back to top
        app.swipeUp()
        pause(0.9)
        mark("sim_demo_journal_scrolled", hold: 1.0)
        app.swipeDown()
        pause(0.8)

        // 1-Tap Text + Photo Compose button in dock (P0-01)
        if tapButton(containing: "打字记一句", timeout: 12, settle: 0.9) {
            mark("sim_compose_sheet", hold: 0.9)
            let composeField = app.textViews.firstMatch
            if composeField.waitForExistence(timeout: 8) {
                if !enterText("今天在真机模拟器里顺手记下妈妈喜欢桂花乌龙茶。", into: composeField) {
                    composeField.typeText("Mom loves osmanthus oolong tea")
                    pause(0.4)
                }
                mark("sim_compose_typed_kb", hold: 0.8)
                _ = tapButton(containing: "收进", timeout: 10, settle: 1.4)
                mark("sim_compose_saved_home", hold: 0.9)
            } else {
                tapBackdropTop()
            }
        }

        // Spotlight Search
        if tapButton(containing: "搜索", timeout: 12, settle: 0.8) {
            let searchField = app.searchFields.firstMatch.exists ? app.searchFields.firstMatch : app.textFields.firstMatch
            if searchField.waitForExistence(timeout: 8) {
                if !enterText("膝盖", into: searchField) {
                    searchField.typeText("tea")
                    pause(0.4)
                }
                pause(0.9)
                mark("sim_spotlight_search", hold: 0.9)
            }
            _ = tapButton(containing: "关闭搜索", timeout: 6, settle: 0.6)
            dismissKeyboardIfNeeded()
        }

        // Top-Right Arrow Popover Alignment (P0 safe-top double-offset fix)
        if tapButton(containing: "轻点切换关系分类", timeout: 12, settle: 0.7) {
            mark("sim_arrow_popover", hold: 0.9)
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.28)).tap()
            pause(0.6)
        }

        // Peek + Edit + Stacked Chinese Lunar Date Wheel ABOVE Peek (P0-03 & P1-10)
        let slipPred = NSPredicate(format: "label CONTAINS[c] '湖边' OR label CONTAINS[c] '桂花' OR label CONTAINS[c] '膝盖' OR label CONTAINS[c] 'osmanthus'")
        let slipCandidate = app.buttons.matching(slipPred).firstMatch.waitForExistence(timeout: 4)
            ? app.buttons.matching(slipPred).firstMatch
            : app.webViews.firstMatch.descendants(matching: .any).matching(slipPred).firstMatch
        if slipCandidate.waitForExistence(timeout: 10) {
            slipCandidate.tap()
            _ = firstButton(containing: "编辑", timeout: 12)
            pause(0.7)
            mark("sim_peek_open", hold: 1.0)
            if tapButton(containing: "编辑", timeout: 10, settle: 0.85) {
                mark("sim_peek_edit_mode", hold: 0.9)
                if tapButton(containing: "202", timeout: 10, settle: 0.9) {
                    mark("sim_date_wheel_stacked_above_peek", hold: 1.1)
                    _ = tapButton(containing: "取消", timeout: 8, settle: 0.65)
                }
                _ = tapButton(containing: "取消", timeout: 8, settle: 0.65)
            }
            tapBackdropTop(settle: 0.85)
        }

        // Vellum Pocket + Lead Days PATCH (`提前 7 天`, P0-04) + Stacked Edit Person (P0-07)
        if tapButton(containing: "人物档案", timeout: 12, settle: 0.9) {
            _ = firstButton(containing: "编辑资料", timeout: 18)
            pause(0.7)
            mark("sim_vellum_pocket", hold: 1.0)
            if tapButton(containing: "提前 7 天", timeout: 8, settle: 1.0) {
                mark("sim_vellum_lead7_patched", hold: 0.9)
            }
            if tapButton(containing: "编辑资料", timeout: 10, settle: 0.9) {
                mark("sim_edit_person_stacked", hold: 1.0)
                _ = tapButton(containing: "返回", timeout: 8, settle: 0.65)
            }
            tapBackdropTop(settle: 0.85)
        }

        // Scale-2 Monthly Gallery + Scale-3 Multi-Year Concentric 12-Month Rings (P1-11 & P1-12)
        if tapButton(containing: "月册", timeout: 12, settle: 1.2) {
            mark("sim_scale2_monthly_gallery", hold: 1.1)
        }
        if tapButton(containing: "年轮", timeout: 12, settle: 1.2) {
            mark("sim_scale3_yearly_rings", hold: 1.1)
        }
        _ = tapButton(containing: "纸页", timeout: 12, settle: 1.0)

        // Inner-4 Frosted Tray + Silver Tier Privacy Veil + People Directory + Recent Archive
        if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 0.9) {
            mark("sim_inner4_frosted_tray", hold: 1.0)
            if tapButton(containing: "陈经理", timeout: 10, settle: 1.6) {
                mark("sim_silver_tier_veil", hold: 1.1)
            }
            if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 0.9) {
                if tapButton(containing: "全部人物", timeout: 10, settle: 1.0) {
                    mark("sim_people_directory", hold: 1.0)
                    if !tapButton(containing: "妈妈", timeout: 10, settle: 1.4) {
                        tapBackdropTop()
                    }
                }
            }
            if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 0.9) {
                if tapButton(containing: "最近归档", timeout: 10, settle: 1.3) {
                    mark("sim_recent_archive", hold: 1.1)
                    tapBackdropTop(settle: 0.85)
                }
            }
        }

        // Cupid Companion Chat + REAL Apple iOS Keyboard + Reply & Citations + Right-Edge Drawer + New Thread
        if tapButton(containing: "丘比特聊聊", timeout: 12, settle: 0.9) {
            _ = firstButton(containing: "新对话", timeout: 18)
            pause(0.8)
            mark("sim_cupid_chat_empty", hold: 1.1)

            let chatInput = app.textViews.firstMatch
            if chatInput.waitForExistence(timeout: 12) {
                chatInput.tap()
                pause(0.9)
                mark("sim_cupid_chat_real_ios_keyboard", hold: 1.0)
                let pasted = enterText("最近关于妈妈有哪些需要提前留心的小事？", into: chatInput)
                if pasted {
                    pause(0.45)
                    _ = tapButton(containing: "发送", timeout: 10, settle: 1.4)
                } else {
                    dismissKeyboardIfNeeded()
                    _ = tapButton(containing: "最近有哪些", timeout: 10, settle: 1.4)
                }
                dismissKeyboardIfNeeded()
                _ = firstButton(containing: "核对手帐出处", timeout: 25)
                pause(0.9)
                mark("sim_cupid_chat_reply_citations", hold: 1.2)
            }

            if tapButton(containing: "拉出手帐抽屉", timeout: 10, settle: 1.0) {
                mark("sim_cupid_right_drawer", hold: 1.1)
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5)).tap()
                pause(0.75)
            }

            if tapButton(containing: "新对话", timeout: 10, settle: 0.9) {
                mark("sim_cupid_new_thread", hold: 1.0)
            }

            _ = tapButton(containing: "回到手帐", timeout: 10, settle: 0.9)
        }

        // Settings + All 3 Distinct Pure Gleb Atmospheres (`晨光白瓷` / `素雅细格` / `柔光珠瓷`)
        if tapButton(containing: "设置", timeout: 12, settle: 0.9) {
            mark("sim_settings_cotton", hold: 1.0)
            if tapButton(containing: "素雅细格", timeout: 8, settle: 0.6) {
                tapBackdropTop(settle: 0.8)
                mark("sim_atmosphere_hobonichi", hold: 1.0)
            }
            if tapButton(containing: "设置", timeout: 10, settle: 0.8) {
                if tapButton(containing: "柔光珠瓷", timeout: 8, settle: 0.6) {
                    tapBackdropTop(settle: 0.8)
                    mark("sim_atmosphere_museglass", hold: 1.0)
                }
            }
            if tapButton(containing: "设置", timeout: 10, settle: 0.8) {
                _ = tapButton(containing: "晨光白瓷", timeout: 8, settle: 0.5)
                tapBackdropTop(settle: 0.8)
                mark("sim_final_morning_glass", hold: 1.0)
            }
        }
    }
}
