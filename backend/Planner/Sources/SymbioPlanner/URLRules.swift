import Foundation
import SymbioCore

/// Python `planner.py` user URL extraction and web navigation policy, with a port of the parts of
/// `urllib.parse.urlsplit` and `ipaddress` they rely on (Foundation's URL parses differently).

/// Extract only URL tokens whose textual boundary is unambiguous. `webURLError` applies the
/// network safety policy to a model-proposed navigation URL later.
func extractUserURLs(_ text: String) -> [String] {
    var urls: [String] = []
    for match in userURLPattern.all(text) {
        let url = unambiguousUserURL(
            match.value, String(text[..<match.range.lowerBound]), String(text[match.range.upperBound...]))
        if let url, !urls.contains(url) { urls.append(url) }
    }
    return urls
}

func hasAmbiguousUserURL(_ text: String) -> Bool {
    userURLPattern.all(text).contains {
        unambiguousUserURL($0.value, String(text[..<$0.range.lowerBound]), String(text[$0.range.upperBound...])) == nil
    }
}

/// Remove only clear prose boundaries; reject ambiguous Korean suffixes.
func unambiguousUserURL(_ token: String, _ precedingText: String, _ followingText: String) -> String? {
    if hasMatchingURLDelimiters(precedingText, followingText) { return token }
    let value = trimClearURLBoundaries(token)
    guard let last = value.unicodeScalars.last else { return nil }
    if !koreanURLParticles.contains(last) { return value }

    let beforeParticle = dropLastScalar(value)
    let candidate = trimClearURLBoundaries(beforeParticle)
    let detachedWrapper = !candidate.unicodeScalars.elementsEqual(beforeParticle.unicodeScalars)
    guard let parsed = try? urlSplit(candidate) else { return nil }
    if ["http", "https"].contains(parsed.scheme), parsed.hostname != nil,
        detachedWrapper || isPlainAsciiRootURL(candidate)
    {
        return candidate
    }
    if startsWithStandaloneParticle(followingText) { return value }
    // A particle-like final character in an IDN, path, query, or fragment
    // cannot be separated without changing the user-authored URL.
    return nil
}

func hasMatchingURLDelimiters(_ precedingText: String, _ followingText: String) -> Bool {
    guard let before = precedingText.unicodeScalars.last, let after = followingText.unicodeScalars.first else {
        return false
    }
    let pairs: [Unicode.Scalar: Unicode.Scalar] = ["<": ">", "[": "]", "{": "}", "「": "」", "『": "』"]
    return pairs[before] == after
}

func startsWithStandaloneParticle(_ text: String) -> Bool {
    pyLStrip(text).unicodeScalars.first.map(koreanURLParticles.contains) ?? false
}

func trimClearURLBoundaries(_ value: String) -> String {
    var value = value
    while let last = value.unicodeScalars.last {
        let rest = dropLastScalar(value)
        if urlSentencePunctuation.contains(last) && isPlainAsciiRootURL(rest) {
            value = rest
            continue
        }
        let scalars = value.unicodeScalars
        if last == ")" && scalars.filter({ $0 == ")" }).count > scalars.filter({ $0 == "(" }).count {
            value = rest
            continue
        }
        if last == "." && isPlainAsciiRootURL(rest) {
            value = rest
            continue
        }
        break
    }
    return value
}

func isPlainAsciiRootURL(_ value: String) -> Bool {
    guard let parsed = try? urlSplit(value), let host = parsed.hostname else { return false }
    return ["http", "https"].contains(parsed.scheme) && host.unicodeScalars.allSatisfy(\.isASCII)
        && (parsed.path.isEmpty || parsed.path == "/") && parsed.query.isEmpty && parsed.fragment.isEmpty
}

/// Python `_web_url_error(raw_url, allow_local=)`.
func webURLError(_ rawURL: JSON?, allowLocal: Bool) -> String? {
    guard let raw = rawURL?.stringValue, !raw.isEmpty else { return "web URL은 비어 있지 않은 문자열이어야 합니다." }
    guard let parsed = try? urlSplit(raw) else { return "web URL을 해석할 수 없습니다." }
    if !["http", "https"].contains(parsed.scheme) { return "web navigation은 http 또는 https URL만 허용합니다." }
    if parsed.username != nil || parsed.password != nil { return "인증정보가 포함된 web URL은 허용하지 않습니다." }
    guard let hostname = parsed.hostname else { return "web URL에 hostname이 필요합니다." }
    if allowLocal { return nil }
    let host = hostname.lowercased()
    if host == "localhost" || host.hasSuffix(".localhost") { return "localhost 탐색은 허용하지 않습니다." }
    guard let address = IPAddress(String(host.split(separator: "%", maxSplits: 1, omittingEmptySubsequences: false)[0]))
    else { return nil }
    if address.isLoopback { return "loopback IP 탐색은 허용하지 않습니다." }
    if address.isLinkLocal { return "link-local IP 탐색은 허용하지 않습니다." }
    return nil
}

// MARK: - urllib.parse.urlsplit (Python 3.12)

struct SplitURL {
    var scheme = "", netloc = "", path = "", query = "", fragment = ""

    /// `SplitResult.hostname`: lowercased, without userinfo, port or brackets.
    var hostname: String? {
        let hostinfo = netloc.components(separatedBy: "@").last ?? ""
        let hostname: String
        if let open = hostinfo.firstIndex(of: "[") {
            let bracketed = hostinfo[hostinfo.index(after: open)...]
            hostname = String(bracketed[..<(bracketed.firstIndex(of: "]") ?? bracketed.endIndex)])
        } else {
            hostname = String(hostinfo[..<(hostinfo.firstIndex(of: ":") ?? hostinfo.endIndex)])
        }
        if hostname.isEmpty { return nil }
        guard let percent = hostname.firstIndex(of: "%") else { return hostname.lowercased() }
        return hostname[..<percent].lowercased() + hostname[percent...]
    }

    private var userinfo: String? {
        guard let at = netloc.range(of: "@", options: .backwards) else { return nil }
        return String(netloc[..<at.lowerBound])
    }
    var username: String? { userinfo.map { String($0[..<($0.firstIndex(of: ":") ?? $0.endIndex)]) } }
    var password: String? {
        guard let info = userinfo, let colon = info.firstIndex(of: ":") else { return nil }
        return String(info[info.index(after: colon)...])
    }
}

struct URLSplitError: Error {}

private let schemeChars = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.".unicodeScalars)
private let futureIPv6 = Pattern(#"\Av[a-fA-F0-9]+\.[^\n]+\z"#)

func urlSplit(_ raw: String) throws(URLSplitError) -> SplitURL {
    var scalars = Array(raw.unicodeScalars.drop { $0.value <= 0x20 })
    scalars.removeAll { $0 == "\t" || $0 == "\r" || $0 == "\n" }
    var result = SplitURL()
    if let colon = scalars.firstIndex(of: ":"), colon > 0, scalars[0].isASCII,
        CharacterSet.letters.contains(scalars[0]), scalars[..<colon].allSatisfy(schemeChars.contains)
    {
        result.scheme = String(String.UnicodeScalarView(scalars[..<colon])).lowercased()
        scalars = Array(scalars[(colon + 1)...])
    }
    if scalars.starts(with: ["/", "/"]) {
        let end = scalars[2...].firstIndex { $0 == "/" || $0 == "?" || $0 == "#" } ?? scalars.endIndex
        result.netloc = String(String.UnicodeScalarView(scalars[2..<end]))
        scalars = Array(scalars[end...])
        let netloc = result.netloc
        if netloc.contains("[") != netloc.contains("]") { throw URLSplitError() }
        if netloc.contains("["), let open = netloc.firstIndex(of: "[") {
            let after = netloc[netloc.index(after: open)...]
            let host = String(after[..<(after.firstIndex(of: "]") ?? after.endIndex)])
            if host.hasPrefix("v") {
                if !futureIPv6.search(host) { throw URLSplitError() }
            } else {
                guard let ip = IPAddress(host), !ip.isV4 else { throw URLSplitError() }
            }
        }
    }
    var rest = String(String.UnicodeScalarView(scalars))
    if let hash = rest.firstIndex(of: "#") {
        result.fragment = String(rest[rest.index(after: hash)...])
        rest = String(rest[..<hash])
    }
    if let question = rest.firstIndex(of: "?") {
        result.query = String(rest[rest.index(after: question)...])
        rest = String(rest[..<question])
    }
    result.path = rest
    // _checknetloc: NFKC must not introduce URL delimiters into a non-ASCII netloc.
    let netloc = result.netloc
    if !netloc.unicodeScalars.allSatisfy(\.isASCII) {
        let n = String(netloc.unicodeScalars.filter { !"@:#?".unicodeScalars.contains($0) })
        let normalized = n.precomposedStringWithCompatibilityMapping
        if !n.unicodeScalars.elementsEqual(normalized.unicodeScalars),
            normalized.unicodeScalars.contains(where: { "/?#@:".unicodeScalars.contains($0) })
        {
            throw URLSplitError()
        }
    }
    return result
}

// MARK: - ipaddress

/// Python `ipaddress.ip_address` for the loopback / link-local checks.
struct IPAddress {
    let bytes: [UInt8]
    var isV4: Bool { bytes.count == 4 }

    init?(_ text: String) {
        if let v4 = IPAddress.parseV4(text) {
            bytes = v4
            return
        }
        let host = String(text[..<(text.firstIndex(of: "%") ?? text.endIndex)])  // scope id
        var address = in6_addr()
        guard text.contains(":"), inet_pton(AF_INET6, host, &address) == 1 else { return nil }
        bytes = withUnsafeBytes(of: &address) { Array($0) }
    }

    /// Strict like Python: four decimal octets, no leading zeros.
    private static func parseV4(_ text: String) -> [UInt8]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var out: [UInt8] = []
        for part in parts {
            guard (1...3).contains(part.count), part.allSatisfy({ ("0"..."9").contains($0) }),
                part.count == 1 || part.first != "0", let value = UInt8(part)
            else { return nil }
            out.append(value)
        }
        return out
    }

    /// An IPv4-mapped IPv6 address is judged by its IPv4 part (stricter than some 3.12 releases).
    private var v4: [UInt8]? {
        if isV4 { return bytes }
        return bytes[..<12].elementsEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF]) ? Array(bytes[12...]) : nil
    }

    var isLoopback: Bool {
        if let v4 { return v4[0] == 127 }
        return bytes == [UInt8](repeating: 0, count: 15) + [1]
    }

    var isLinkLocal: Bool {
        if let v4 { return v4[0] == 169 && v4[1] == 254 }
        return bytes[0] == 0xFE && bytes[1] & 0xC0 == 0x80
    }
}
