import UIKit
import XCTest

/// Real Apple Xcode iOS Simulator end-to-end hands-on user evaluation for MindWeave (心络).
/// Drives the compiled `MindWeave.app` (`WKWebView` + `MindWeaveSchemeHandler` + Swift bridges)
/// inside a real iOS Simulator (`iPhone 16 Pro`) using genuine touch taps, real iOS software
/// keyboard input, scrolling, stacked sheets, and multi-atmosphere switching while `simctl`
/// continuously records full-motion 60fps H.264 video and lossless Retina screenshots.
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

        func snap(_ name: String) {
            stepIndex += 1
            let prefix = String(format: "%02d", stepIndex)
            let shot = XCUIScreen.main.screenshot()
            let attachment = XCTAttachment(screenshot: shot)
            attachment.name = "\(prefix)_\(name)"
            attachment.lifetime = .keepAlways
            add(attachment)
        }

        func pause(_ seconds: TimeInterval = 0.75) {
            Thread.sleep(forTimeInterval: seconds)
        }

        func firstButton(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let el = app.buttons.matching(pred).firstMatch
            _ = el.waitForExistence(timeout: timeout)
            return el
        }

        func firstStaticText(containing text: String, timeout: TimeInterval = 22) -> XCUIElement {
            let pred = NSPredicate(format: "label CONTAINS[c] %@", text)
            let el = app.staticTexts.matching(pred).firstMatch
            _ = el.waitForExistence(timeout: timeout)
            return el
        }

        @discardableResult
        func tapButton(containing text: String, timeout: TimeInterval = 22, settle: TimeInterval = 0.9) -> Bool {
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

        func tapBackdropTop(settle: TimeInterval = 0.85) {
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

        /// Enters ASCII via real iOS software keyboard keystrokes, and CJK safely via UIPasteboard paste
        /// so English-default iOS Simulator keyboards never crash trying to synthesize CJK keycodes.
        @discardableResult
        func enterText(_ text: String, into element: XCUIElement) -> Bool {
            guard element.waitForExistence(timeout: 12) else { return false }
            element.tap()
            pause(0.55)
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

        func dismissKeyboardTutorialIfNeeded() {
            for label in ["Continue", "继续", "Not Now", "以后再说"] {
                let btn = app.buttons[label]
                if btn.exists && btn.isHittable {
                    btn.tap()
                    pause(0.6)
                    return
                }
            }
        }

        func loginWithEmail(_ email: String, snapSplash: Bool) {
            let emailField = app.textFields.firstMatch
            XCTAssertTrue(emailField.waitForExistence(timeout: 30), "Auth email input should appear on splash")
            pause(1.8)
            if snapSplash {
                snap("sim_splash_lakeside")
            }

            emailField.tap()
            pause(1.0)
            dismissKeyboardTutorialIfNeeded()
            pause(0.5)
            if snapSplash {
                snap("sim_splash_ios_keyboard")
            }

            if let currentVal = emailField.value as? String, !currentVal.isEmpty, currentVal != "输入你的邮箱" {
                let deleteString = String(repeating: XCUIKeyboardKey.delete.rawValue, count: currentVal.count + 4)
                emailField.typeText(deleteString)
                pause(0.25)
            }
            emailField.typeText(email)
            pause(0.5)

            XCTAssertTrue(tapButton(containing: "获取验证码", timeout: 15, settle: 1.0), "Should tap 获取验证码")
            let verifyBtn = firstButton(containing: "翻开手帐", timeout: 30)
            XCTAssertTrue(verifyBtn.exists, "OTP step 翻开手帐 button should appear")
            pause(0.5)
            if snapSplash {
                snap("sim_otp_step")
            }

            verifyBtn.tap()
            pause(1.5)
        }

        // Launch MindWeave.app inside the real Apple iOS Simulator!
        app.launch()

        // =====================================================================
        // PART 1: COLD-START BRAND-NEW USER (Splash -> Real iOS Keyboard -> OTP -> 3-Step Onboarding)
        // =====================================================================
        loginWithEmail("newuser_sim@example.com", snapSplash: true)

        let step1Heading = firstStaticText(containing: "把重要的人", timeout: 30)
        XCTAssertTrue(step1Heading.exists, "New user should land on Onboarding Step 1")
        pause(0.6)
        snap("sim_onb_step1")

        let exampleChip = firstButton(containing: "妈妈", timeout: 15)
        if exampleChip.exists {
            exampleChip.tap()
            pause(0.7)
        }
        XCTAssertTrue(tapButton(containing: "记下这句", timeout: 15, settle: 1.0), "Should submit Step 1 sentence")

        // Wait generously for /v1/captures on loaded macOS CI runner before snapping Step 2
        let goldArrowBtn = firstButton(containing: "金箭", timeout: 35)
        XCTAssertTrue(goldArrowBtn.exists, "Onboarding Step 2 Gold arrow card should appear")
        pause(0.6)
        snap("sim_onb_step2")

        goldArrowBtn.tap()
        pause(1.2)

        // Wait generously for Step 3 live Cupid reply & enter journal CTA
        let enterJournalBtn = firstButton(containing: "翻开", timeout: 35)
        XCTAssertTrue(enterJournalBtn.exists, "Onboarding Step 3 enter journal button should appear")
        pause(0.8)
        snap("sim_onb_step3_cupid")

        enterJournalBtn.tap()
        _ = firstButton(containing: "设置", timeout: 30)
        pause(1.2)
        snap("sim_newuser_journal_home")

        // Sign out via Settings -> switch to rich multi-year seeded demo user
        XCTAssertTrue(tapButton(containing: "设置", timeout: 15, settle: 1.0), "Should open Settings")
        XCTAssertTrue(tapButton(containing: "退出登录", timeout: 15, settle: 1.5), "Should sign out to splash")

        // =====================================================================
        // PART 2: RICH MULTI-YEAR DEMO USER DAILY LIFE (7 Personas, Gold/Silver/Crystal, Photos, Reminders, Pencil Drafts)
        // =====================================================================
        loginWithEmail("demo@mindweave.app", snapSplash: false)

        _ = firstButton(containing: "人物档案", timeout: 35)
        pause(1.5)
        snap("sim_demo_journal_home")

        // Real iOS finger scroll through photo slip, voice memo, pencil draft, then back up
        app.swipeUp()
        pause(1.0)
        snap("sim_demo_journal_scrolled")
        app.swipeDown()
        pause(0.9)

        // 1-Tap Text + Photo Compose button in dock (P0-01)
        if tapButton(containing: "打字记一句", timeout: 8, settle: 0.9) {
            snap("sim_compose_sheet")
            let composeField = app.textViews.firstMatch
            if composeField.waitForExistence(timeout: 5) {
                let pasted = enterText("今天在真机模拟器里顺手记下妈妈喜欢桂花乌龙茶。", into: composeField)
                if !pasted {
                    composeField.typeText("Mom loves osmanthus oolong tea")
                    pause(0.4)
                }
                snap("sim_compose_typed_kb")
                _ = tapButton(containing: "收进", timeout: 6, settle: 1.6)
                snap("sim_compose_saved_home")
            } else {
                tapBackdropTop()
            }
        }

        // Spotlight Search
        if tapButton(containing: "搜索", timeout: 8, settle: 0.8) {
            let searchField = app.searchFields.firstMatch.exists ? app.searchFields.firstMatch : app.textFields.firstMatch
            if searchField.waitForExistence(timeout: 5) {
                if !enterText("膝盖", into: searchField) {
                    searchField.typeText("tea")
                    pause(0.4)
                }
                pause(1.0)
                snap("sim_spotlight_search")
            }
            _ = tapButton(containing: "关闭搜索", timeout: 4, settle: 0.65)
            dismissKeyboardIfNeeded()
        }

        // Top-Right Arrow Popover Alignment (P0 safe-top double-offset fix)
        if tapButton(containing: "轻点切换关系分类", timeout: 8, settle: 0.75) {
            snap("sim_arrow_popover")
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.28)).tap()
            pause(0.6)
        }

        // Peek + Edit + Stacked Chinese Lunar Date Wheel ABOVE Peek (P0-03 & P1-10)
        // Note: WKWebView exposes <article role="button"> slips as `app.buttons`!
        let slipCandidate = app.buttons.matching(
            NSPredicate(format: "label CONTAINS[c] '湖边' OR label CONTAINS[c] '桂花' OR label CONTAINS[c] '膝盖' OR label CONTAINS[c] 'osmanthus'")
        ).firstMatch
        if slipCandidate.waitForExistence(timeout: 12) {
            slipCandidate.tap()
            _ = firstButton(containing: "编辑", timeout: 12)
            pause(0.8)
            snap("sim_peek_open")
            if tapButton(containing: "编辑", timeout: 10, settle: 0.9) {
                snap("sim_peek_edit_mode")
                if tapButton(containing: "202", timeout: 10, settle: 1.0) {
                    snap("sim_date_wheel_stacked_above_peek")
                    _ = tapButton(containing: "取消", timeout: 8, settle: 0.7)
                }
                _ = tapButton(containing: "取消", timeout: 8, settle: 0.7)
            }
            tapBackdropTop(settle: 0.9)
        }

        // Vellum Pocket + Lead Days PATCH (`提前 7 天`, P0-04) + Stacked Edit Person (P0-07)
        if tapButton(containing: "人物档案", timeout: 12, settle: 1.0) {
            _ = firstButton(containing: "编辑资料", timeout: 18)
            pause(0.8)
            snap("sim_vellum_pocket")
            if tapButton(containing: "提前 7 天", timeout: 8, settle: 1.2) {
                snap("sim_vellum_lead7_patched")
            }
            if tapButton(containing: "编辑资料", timeout: 10, settle: 1.0) {
                snap("sim_edit_person_stacked")
                _ = tapButton(containing: "返回", timeout: 8, settle: 0.7)
            }
            tapBackdropTop(settle: 0.9)
        }

        // Scale-2 Monthly Gallery + Scale-3 Multi-Year Concentric 12-Month Rings (P1-11 & P1-12)
        if tapButton(containing: "月册", timeout: 12, settle: 1.5) {
            snap("sim_scale2_monthly_gallery")
        }
        if tapButton(containing: "年轮", timeout: 12, settle: 1.5) {
            snap("sim_scale3_yearly_rings")
        }
        _ = tapButton(containing: "纸页", timeout: 12, settle: 1.2)

        // Inner-4 Frosted Tray + Silver Tier Privacy Veil + People Directory + Recent Archive
        if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 1.0) {
            snap("sim_inner4_frosted_tray")
            if tapButton(containing: "陈经理", timeout: 10, settle: 2.0) {
                snap("sim_silver_tier_veil")
            }
            if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 1.0) {
                if tapButton(containing: "全部人物", timeout: 10, settle: 1.2) {
                    snap("sim_people_directory")
                    if !tapButton(containing: "妈妈", timeout: 10, settle: 1.6) {
                        tapBackdropTop()
                    }
                }
            }
            if tapButton(containing: "轻点展开人物栏", timeout: 12, settle: 1.0) {
                if tapButton(containing: "最近归档", timeout: 10, settle: 1.6) {
                    snap("sim_recent_archive")
                    tapBackdropTop(settle: 0.9)
                }
            }
        }

        // Cupid Companion Chat + REAL Apple iOS Keyboard + Reply & Citations + Right-Edge Drawer + New Thread
        if tapButton(containing: "丘比特聊聊", timeout: 12, settle: 1.0) {
            _ = firstButton(containing: "新对话", timeout: 18)
            pause(0.9)
            snap("sim_cupid_chat_empty")

            let chatInput = app.textViews.firstMatch
            if chatInput.waitForExistence(timeout: 12) {
                chatInput.tap()
                pause(1.0)
                snap("sim_cupid_chat_real_ios_keyboard")
                let pasted = enterText("最近关于妈妈有哪些需要提前留心的小事？", into: chatInput)
                if pasted {
                    pause(0.5)
                    _ = tapButton(containing: "发送", timeout: 10, settle: 1.5)
                } else {
                    dismissKeyboardIfNeeded()
                    _ = tapButton(containing: "最近有哪些", timeout: 10, settle: 1.5)
                }
                dismissKeyboardIfNeeded()
                // Wait for streaming reply citation button to appear cleanly
                _ = firstButton(containing: "核对手帐出处", timeout: 25)
                pause(1.0)
                snap("sim_cupid_chat_reply_citations")
            }

            if tapButton(containing: "拉出手帐抽屉", timeout: 10, settle: 1.2) {
                snap("sim_cupid_right_drawer")
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.08, dy: 0.5)).tap()
                pause(0.8)
            }

            if tapButton(containing: "新对话", timeout: 10, settle: 1.0) {
                snap("sim_cupid_new_thread")
            }

            _ = tapButton(containing: "回到手帐", timeout: 10, settle: 1.0)
        }

        // Settings + All 3 Distinct Pure Gleb Atmospheres (`晨光白瓷` / `素雅细格` / `柔光珠瓷`)
        if tapButton(containing: "设置", timeout: 8, settle: 0.95) {
            snap("sim_settings_cotton")
            if tapButton(containing: "素雅细格", timeout: 6, settle: 0.65) {
                tapBackdropTop(settle: 0.85)
                snap("sim_atmosphere_hobonichi")
            }
            if tapButton(containing: "设置", timeout: 8, settle: 0.85) {
                if tapButton(containing: "柔光珠瓷", timeout: 6, settle: 0.65) {
                    tapBackdropTop(settle: 0.85)
                    snap("sim_atmosphere_museglass")
                }
            }
            if tapButton(containing: "设置", timeout: 8, settle: 0.85) {
                _ = tapButton(containing: "晨光白瓷", timeout: 6, settle: 0.55)
                tapBackdropTop(settle: 0.85)
                snap("sim_final_morning_glass")
            }
        }
    }
}
