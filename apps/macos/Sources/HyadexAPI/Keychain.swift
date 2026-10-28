import Foundation
import Security

/// Access tokens live in the login keychain, one item per server.
public enum TokenStore {
    // Keep development/acceptance bundles out of the user's saved connections.
    // The normal bundle retains the original keychain service name.
    private static let service = (Bundle.main.bundleIdentifier ?? "dev.hyadex.mac") + ".server-token"

    public static func token(for server: UUID) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.uuidString,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public static func setToken(_ token: String?, for server: UUID) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.uuidString,
        ]
        SecItemDelete(base as CFDictionary)
        guard let token, !token.isEmpty else { return }
        var item = base
        item[kSecValueData as String] = Data(token.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(item as CFDictionary, nil)
    }
}
