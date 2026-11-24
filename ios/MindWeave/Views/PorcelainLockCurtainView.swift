import SwiftUI

/// Opaque warm porcelain (`#FCFBF9`) privacy curtain shielding MindWeave's private journals
/// during App Switcher snapshots (`.inactive`) and Face ID / Touch ID / Passcode lock (`.locked`).
/// Zero heavy GPU blur — instant 0ms composition matching MindWeave's V2 porcelain canvas.
@MainActor
struct PorcelainLockCurtainView: View {
    @ObservedObject var lockManager: BiometricLockManager

    private static let porcelainBg = Color(red: 0.988, green: 0.984, blue: 0.976) // #FCFBF9
    private static let warmInk = Color(red: 0.200, green: 0.173, blue: 0.153)     // #332C27
    private static let mutedInk = Color(red: 0.478, green: 0.431, blue: 0.392)    // #7A6E64
    private static let hairline = Color(red: 0.200, green: 0.173, blue: 0.153).opacity(0.10)
    private static let pillSurface = Color.white.opacity(0.92)

    var body: some View {
        ZStack {
            Self.porcelainBg
                .ignoresSafeArea()

            VStack(spacing: 18) {
                Spacer()

                ZStack {
                    Circle()
                        .fill(Self.pillSurface)
                        .frame(width: 72, height: 72)
                        .overlay(
                            Circle()
                                .stroke(Self.hairline, lineWidth: 1)
                        )
                    Text("心络")
                        .font(.system(size: 22, weight: .semibold, design: .serif))
                        .foregroundStyle(Self.warmInk)
                }

                VStack(spacing: 6) {
                    Text(lockManager.isLocked ? "心络已为你收好" : "心络")
                        .font(.system(size: 19, weight: .medium, design: .serif))
                        .foregroundStyle(Self.warmInk)

                    if lockManager.isLocked {
                        Text("你的手帐仅限本人翻阅")
                            .font(.system(size: 13))
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
                                .font(.system(size: 15, weight: .medium))
                        }
                        .foregroundStyle(Self.warmInk)
                        .padding(.horizontal, 22)
                        .padding(.vertical, 12)
                        .background(Self.pillSurface)
                        .clipShape(Capsule())
                        .overlay(
                            Capsule()
                                .stroke(Self.hairline, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(lockManager.isAuthenticating)
                    .padding(.top, 8)

                    Text("轻点解锁，或使用设备密码")
                        .font(.system(size: 12))
                        .foregroundStyle(Self.mutedInk)
                        .padding(.top, 2)

                    if let err = lockManager.lastErrorMessage, !err.isEmpty {
                        Text(err)
                            .font(.system(size: 12))
                            .foregroundStyle(Self.mutedInk)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 36)
                            .padding(.top, 4)
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
