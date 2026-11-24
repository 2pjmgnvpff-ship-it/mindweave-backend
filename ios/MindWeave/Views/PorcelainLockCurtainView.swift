import SwiftUI

/// Luminous Gleb Morning-Light privacy curtain shielding MindWeave's private journals
/// during App Switcher snapshots (`.inactive`) and Face ID / Touch ID / Passcode lock (`.locked`).
@MainActor
struct PorcelainLockCurtainView: View {
    @ObservedObject var lockManager: BiometricLockManager

    private static let porcelainBg = Color(red: 0.980, green: 0.965, blue: 0.941) // #FAF6F0
    private static let peachGlow = Color(red: 1.0, green: 0.776, blue: 0.604).opacity(0.55)
    private static let goldGlow = Color(red: 0.988, green: 0.894, blue: 0.729).opacity(0.45)
    private static let warmInk = Color(red: 0.145, green: 0.122, blue: 0.106)     // #251F1B
    private static let mutedInk = Color(red: 0.435, green: 0.392, blue: 0.357)    // #6F645B
    private static let accentStart = Color(red: 0.302, green: 0.231, blue: 0.196)
    private static let accentEnd = Color(red: 0.204, green: 0.153, blue: 0.129)

    var body: some View {
        ZStack {
            Self.porcelainBg.ignoresSafeArea()

            RadialGradient(
                colors: [Self.peachGlow, .clear],
                center: UnitPoint(x: 0.92, y: 0.08),
                startRadius: 12,
                endRadius: 340
            )
            .ignoresSafeArea()

            RadialGradient(
                colors: [Self.goldGlow, .clear],
                center: UnitPoint(x: 0.10, y: 0.88),
                startRadius: 12,
                endRadius: 280
            )
            .ignoresSafeArea()

            VStack(spacing: 20) {
                Spacer()

                ZStack {
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [.white, Color(red: 0.984, green: 0.875, blue: 0.776)],
                                center: UnitPoint(x: 0.34, y: 0.28),
                                startRadius: 4,
                                endRadius: 48
                            )
                        )
                        .frame(width: 82, height: 82)
                        .overlay(
                            Circle()
                                .stroke(Color.white.opacity(0.95), lineWidth: 2)
                        )
                        .shadow(color: Color(red: 0.85, green: 0.56, blue: 0.36).opacity(0.28), radius: 22, x: 0, y: 10)

                    Text("心络")
                        .font(.system(size: 22, weight: .semibold, design: .serif))
                        .foregroundStyle(Self.warmInk)
                }

                VStack(spacing: 6) {
                    Text(lockManager.isLocked ? "心络已为你收好" : "心络")
                        .font(.system(size: 20, weight: .semibold, design: .serif))
                        .foregroundStyle(Self.warmInk)

                    if lockManager.isLocked {
                        Text("你的手帐仅限本人翻阅")
                            .font(.system(size: 13.5))
                            .foregroundStyle(Self.mutedInk)
                    }
                }

                if lockManager.isLocked {
                    Button {
                        lockManager.triggerUnlock(userInitiated: true)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: iconName(for: lockManager.cachedBiometryKind))
                                .font(.system(size: 15, weight: .medium))
                            Text(unlockButtonLabel)
                                .font(.system(size: 15, weight: .semibold))
                        }
                        .foregroundStyle(Color(red: 1.0, green: 0.984, blue: 0.969))
                        .padding(.horizontal, 26)
                        .padding(.vertical, 13)
                        .background(
                            LinearGradient(colors: [Self.accentStart, Self.accentEnd], startPoint: .topLeading, endPoint: .bottomTrailing)
                        )
                        .clipShape(Capsule())
                        .shadow(color: Self.accentEnd.opacity(0.24), radius: 14, x: 0, y: 6)
                    }
                    .buttonStyle(.plain)
                    .disabled(lockManager.isAuthenticating)
                    .padding(.top, 8)

                    Text("轻点解锁，或使用设备密码")
                        .font(.system(size: 12))
                        .foregroundStyle(Self.mutedInk)

                    if let err = lockManager.lastErrorMessage, !err.isEmpty {
                        Text(err)
                            .font(.system(size: 12))
                            .foregroundStyle(Self.mutedInk)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 36)
                            .padding(.top, 2)
                    }
                }

                Spacer()
            }
            .padding(.horizontal, 24)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if lockManager.isLocked && !lockManager.isAuthenticating {
                lockManager.triggerUnlock(userInitiated: true)
            }
        }
    }

    private var unlockButtonLabel: String {
        switch lockManager.cachedBiometryKind {
        case "faceID", "touchID", "opticID":
            return "\(lockManager.cachedBiometryLabelCn) 解锁"
        default:
            return "解锁手帐"
        }
    }

    private func iconName(for kind: String) -> String {
        switch kind {
        case "faceID": return "faceid"
        case "touchID": return "touchid"
        case "opticID": return "opticid"
        default: return "lock.fill"
        }
    }
}
