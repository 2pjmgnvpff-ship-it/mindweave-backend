import Foundation
import SwiftUI

/// Gleb-warm native SwiftUI sheet for configuring and verifying the MindWeave backend server URL.
@MainActor
struct ServerConfigSheet: View {
    private enum ConnectionTestState: Equatable {
        case idle
        case testing
        case reachable(statusCode: Int, latencyMs: Int, note: String?)
        case unreachable(String)
    }

    @ObservedObject var config: ServerConfigStore
    @Environment(\.dismiss) private var dismiss

    @State private var draftURL: String
    @State private var testState: ConnectionTestState = .idle
    @State private var validationMessage: String?

    private static let cottonPaper = Color(red: 0.969, green: 0.937, blue: 0.890)
    private static let cardSurface = Color.white.opacity(0.78)
    private static let warmInk = Color(red: 0.231, green: 0.184, blue: 0.165)
    private static let mutedInk = Color(red: 0.478, green: 0.416, blue: 0.369)
    private static let champagneGold = Color(red: 0.722, green: 0.525, blue: 0.294)
    private static let sageGreen = Color(red: 0.235, green: 0.569, blue: 0.408)
    private static let warmAmber = Color(red: 0.808, green: 0.451, blue: 0.239)

    init(config: ServerConfigStore) {
        self.config = config
        self._draftURL = State(initialValue: config.apiBaseURLString)
    }

    private var appVersionLabel: String {
        let shortVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0.0"
        let buildNumber = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "1"
        return "v\(shortVersion) (\(buildNumber))"
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Self.cottonPaper
                    .ignoresSafeArea()

                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        headerBanner
                        serverInputCard
                        statusCard
                        actionButtons
                        footerHint
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 14)
                    .padding(.bottom, 28)
                }
            }
            .navigationTitle("服务器连接")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("关闭") {
                        dismiss()
                    }
                    .foregroundStyle(Self.warmInk)
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .preferredColorScheme(.light)
    }

    private var headerBanner: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("心络后端地址")
                .font(.system(size: 20, weight: .semibold, design: .serif))
                .foregroundStyle(Self.warmInk)

            Text("填写云端 Cloud Run 地址或同一局域网下的本机服务地址（如 http://192.168.x.x:8000），本地页面将自动通过安全通道代理请求。")
                .font(.system(size: 13))
                .foregroundStyle(Self.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var serverInputCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("API 地址")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Self.mutedInk)

            TextField("https://mindweave-backend-kou46qyjxa-de.a.run.app", text: $draftURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled(true)
                .font(.system(size: 15, design: .monospaced))
                .foregroundStyle(Self.warmInk)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Self.cottonPaper.opacity(0.65))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .stroke(Self.champagneGold.opacity(0.32), lineWidth: 1)
                )

            if let validationMessage {
                Text(validationMessage)
                    .font(.system(size: 12))
                    .foregroundStyle(Self.warmAmber)
            }

            HStack(spacing: 10) {
                presetChip(title: "云端默认", url: ServerConfigStore.defaultBaseURLString)
                presetChip(title: "本机调试", url: "http://127.0.0.1:8000")
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Self.cardSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.white.opacity(0.9), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.04), radius: 14, x: 0, y: 6)
    }

    private func presetChip(title: String, url: String) -> some View {
        Button {
            NativeHaptics.shared.trigger("selection")
            draftURL = url
            validationMessage = nil
            testState = .idle
        } label: {
            Text(title)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Self.warmInk)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(
                    Capsule(style: .continuous)
                        .fill(Self.cottonPaper)
                )
                .overlay(
                    Capsule(style: .continuous)
                        .stroke(Self.champagneGold.opacity(0.35), lineWidth: 1)
                )
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var statusCard: some View {
        HStack(spacing: 12) {
            statusIndicator
            VStack(alignment: .leading, spacing: 3) {
                Text(statusHeadline)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(Self.warmInk)
                Text(statusDetail)
                    .font(.system(size: 12))
                    .foregroundStyle(Self.mutedInk)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Self.cardSurface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.white.opacity(0.85), lineWidth: 1)
        )
    }

    @ViewBuilder
    private var statusIndicator: some View {
        switch testState {
        case .idle:
            Circle()
                .fill(Self.champagneGold.opacity(0.55))
                .frame(width: 10, height: 10)
        case .testing:
            ProgressView()
                .tint(Self.champagneGold)
                .scaleEffect(0.85)
                .frame(width: 16, height: 16)
        case .reachable:
            Circle()
                .fill(Self.sageGreen)
                .frame(width: 10, height: 10)
                .shadow(color: Self.sageGreen.opacity(0.45), radius: 5)
        case .unreachable:
            Circle()
                .fill(Self.warmAmber)
                .frame(width: 10, height: 10)
                .shadow(color: Self.warmAmber.opacity(0.45), radius: 5)
        }
    }

    private var statusHeadline: String {
        switch testState {
        case .idle:
            return "尚未测试连接"
        case .testing:
            return "正在检测 /healthz …"
        case let .reachable(code, latencyMs, _):
            return "连接正常 · HTTP \(code) · \(latencyMs) ms"
        case .unreachable:
            return "暂时无法连接到服务器"
        }
    }

    private var statusDetail: String {
        switch testState {
        case .idle:
            return "当前生效：\(config.apiBaseURLString)"
        case .testing:
            return "正在验证后端健康检查接口"
        case let .reachable(_, _, note):
            return note ?? "后端服务已就绪，可以保存并重新加载手帐。"
        case let .unreachable(reason):
            return reason
        }
    }

    private var actionButtons: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                Button {
                    Task {
                        await runHealthCheck()
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "waveform.path.ecg")
                            .font(.system(size: 14, weight: .medium))
                        Text("测试连接")
                            .font(.system(size: 15, weight: .medium))
                    }
                    .foregroundStyle(Self.warmInk)
                    .frame(maxWidth: .infinity)
                    .frame(height: 48)
                    .background(
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .fill(Self.cardSurface)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .stroke(Self.champagneGold.opacity(0.42), lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)
                .disabled(testState == .testing)

                Button {
                    saveAndReload()
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 15, weight: .medium))
                        Text("保存并重载")
                            .font(.system(size: 15, weight: .semibold))
                    }
                    .foregroundStyle(Color.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 48)
                    .background(
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .fill(Self.warmInk)
                    )
                }
                .buttonStyle(.plain)
            }

            Button {
                NativeHaptics.shared.trigger("light")
                config.resetToDefault()
                draftURL = config.apiBaseURLString
                validationMessage = nil
                testState = .idle
                dismiss()
            } label: {
                Text("恢复默认服务器地址")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Self.mutedInk)
                    .frame(maxWidth: .infinity)
                    .frame(height: 44)
            }
            .buttonStyle(.plain)
        }
    }

    private var footerHint: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("支持一键链接配置：mindweave://server?url=你的服务地址")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(Self.mutedInk.opacity(0.85))
            Text("心络 iOS · \(appVersionLabel)")
                .font(.system(size: 11))
                .foregroundStyle(Self.mutedInk.opacity(0.7))
        }
        .padding(.top, 4)
    }

    private func saveAndReload() {
        guard config.updateBaseURL(draftURL) else {
            validationMessage = "请输入有效的 http:// 或 https:// 服务器地址"
            NativeHaptics.shared.trigger("warning")
            return
        }
        validationMessage = nil
        NativeHaptics.shared.trigger("success")
        dismiss()
    }

    private func runHealthCheck() async {
        guard let normalized = ServerConfigStore.normalizeURLString(draftURL),
              let baseURL = URL(string: normalized),
              let healthURL = MindWeaveSchemeHandler.buildUpstreamURL(
                  for: URL(string: "mindweave://app/healthz")!,
                  baseURL: baseURL
              ) else {
            validationMessage = "请输入有效的 http:// 或 https:// 服务器地址"
            testState = .unreachable("地址格式无效，请检查协议与域名。")
            NativeHaptics.shared.trigger("warning")
            return
        }

        validationMessage = nil
        draftURL = normalized
        testState = .testing

        var request = URLRequest(url: healthURL)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 8
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("ios-wkwebview", forHTTPHeaderField: "X-Client-Platform")

        let start = CFAbsoluteTimeGetCurrent()
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let latencyMs = max(1, Int((CFAbsoluteTimeGetCurrent() - start) * 1000))
            guard let http = response as? HTTPURLResponse else {
                testState = .unreachable("收到非 HTTP 响应，请检查地址。")
                NativeHaptics.shared.trigger("warning")
                return
            }

            if (200...299).contains(http.statusCode) {
                let note = parseHealthNote(from: data)
                testState = .reachable(statusCode: http.statusCode, latencyMs: latencyMs, note: note)
                NativeHaptics.shared.trigger("success")
            } else {
                testState = .unreachable("健康检查返回 HTTP \(http.statusCode)，请确认服务已启动。")
                NativeHaptics.shared.trigger("warning")
            }
        } catch {
            testState = .unreachable("无法访问 \(normalized)/healthz，请确认网络或局域网端口可达。")
            NativeHaptics.shared.trigger("error")
        }
    }

    private func parseHealthNote(from data: Data) -> String? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        if let status = json["status"] as? String, !status.isEmpty {
            return "服务状态：\(status)"
        }
        return nil
    }
}
