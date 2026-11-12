// Staged local file search for requests whose file name is unclear (Python: file_search.py).
//
// The model only proposes search words. This module walks the allowed folders itself and widens
// the search in fixed steps: file name, then folder path, then the start of text files. It never
// follows symlinks and never hands file content to the model; the caller reads a chosen file
// through the normal read tools. The result also says whether to proceed, list a few
// candidates, or ask the user to narrow a long list.

import Foundation
import SymbioCore

public let findFilesTool = "local__find_files"
public let listFilesTool = "local__list_files"
public let groupFilesTool = "local__group_files"
public let maxShow = 5  // more candidates than this are never listed; the user narrows instead
public let maxKeywords = 6
public let maxFacets = 3
public let listMax = 50
public let maxGroups = 8
let excludedDirs: Set<String> = ["node_modules", "__pycache__", "venv"]
let textExtensions: Set<String> = ["txt", "md", "csv", "tsv", "json", "rst", "log", "yaml", "yml"]
// Documents whose text this search does not look into (only their name, folder and dates).
let documentExtensions: Set<String> = ["pdf", "docx", "doc", "rtf", "odt", "html", "htm"]
let filterKeys = ["extensions", "folder_hint", "modified_after", "modified_before", "content_keywords"]
let kinds: [(String, Set<String>)] = [
    ("이미지", ["jpg", "jpeg", "png", "gif", "heic", "webp", "bmp", "tiff", "svg"]),
    ("문서", ["pdf", "doc", "docx", "hwp", "hwpx", "txt", "md", "rtf", "odt", "pages", "ppt", "pptx", "key"]),
    ("표", ["xls", "xlsx", "csv", "tsv", "numbers"]),
    ("압축", ["zip", "rar", "7z", "tar", "gz", "dmg"]),
    ("동영상", ["mp4", "mov", "avi", "mkv"]),
    ("음악", ["mp3", "wav", "m4a", "aac", "flac"]),
]

/// Bounds for a folder tree that grows over time; hitting one marks the result truncated.
/// Python module constants (tests monkeypatch them; here they are passed in).
public struct SearchLimits: Sendable {
    public var maxDepth = 8
    public var maxVisited = 20000
    public var contentMaxFiles = 300
    public var contentBytes = 64 * 1024
    public var contentSeconds = 5.0
    public var walkSeconds = 10.0
    public var groupShow = 20  // paths per group handed to the model; the count says how many
    public init() {}
}

public let findFilesDefinition = ToolDefinition(
    name: findFilesTool,
    description: "파일명이 불분명할 때 허용된 폴더 안에서 파일을 찾는다. 파일명 → 폴더 경로 → 텍스트 내용 "
        + "순서로 앱이 알아서 넓혀 가며 찾으므로 keywords에 파일명·주제어와 한/영 변형(예: 회의록, "
        + "meeting, minutes)을 넣는다. 각 keyword는 대체어이며 하나만 맞아도 된다. "
        + "extensions·folder_hint·modified_*는 사용자가 형식·폴더·날짜를 말했을 때만 쓴다. "
        + "folder_hint는 폴더 이름이지 절대경로가 아니다. content_keywords는 파일 이름이 아니라 "
        + "본문에 들어 있어야 할 말(예: 예산 승인)로, 이름·폴더로 찾은 후보 안에서만 본문을 확인해 "
        + "좁힌다. 앱은 사용자가 지정한 조건을 스스로 풀지 않는다. 조건 때문에 결과가 없으면 "
        + "사용자가 말한 조건은 그대로 두고 알리고, 추측해 넣은 조건만 빼고 다시 호출할 수 있다. "
        + "결과의 decision이 single이면 그 파일을 쓰고, choose·narrow는 앱이 사용자에게 직접 "
        + "묻는다. complete가 false면 limits에 적힌 검사하지 못한 범위를 사용자에게 함께 알린다.",
    schema: [
        "type": "object",
        // No size limits here: a provider checks a generated call against this schema before we
        // ever see it and answers HTTP 400 for "9 items, want 8". The limits are applied in code.
        "properties": [
            "keywords": ["type": "array", "items": ["type": "string"]],
            "extensions": ["type": ["array", "null"], "items": ["type": "string"]],
            "content_keywords": ["type": ["array", "null"], "items": ["type": "string"]],
            "folder_hint": ["type": ["string", "null"]],
            "modified_after": ["type": ["string", "null"]],
            "modified_before": ["type": ["string", "null"]],
        ],
        "required": ["keywords"],
    ]
)

public let listDefinition = ToolDefinition(
    name: listFilesTool,
    description: "허용된 폴더 안 파일을 크기·수정일과 함께 목록으로 본다(하위 폴더 포함). 가장 큰 파일, "
        + "최근에 바뀐 파일, 며칠 안/며칠 넘은 파일, 형식별 목록, 폴더별 용량처럼 크기·날짜로 "
        + "고르거나 정렬하는 요청에 쓴다. sort_by는 size·modified·name(기본 modified, 큰 것·최근 "
        + "것 먼저, ascending이 true면 반대). name_contains는 파일 이름에 들어갈 말(하나만 맞아도 "
        + "됨)이다. paths는 이미 찾은 후보 경로만 볼 때 쓴다. "
        + "modified_within_days=N은 최근 N일 안에 바뀐 파일, "
        + "not_modified_for_days=N은 N일 넘게 안 바뀐 파일이다. group_by가 folder면 최상위 "
        + "폴더별 합계 용량을 큰 순서로 준다. 결과는 사용자에게 보이지 않으므로 답에 이름을 "
        + "결과 순서대로 직접 적고, 바이트 크기는 size_bytes를 쓴다. 안 쓰는 인자는 생략한다.",
    schema: [
        "type": "object",
        "properties": [
            "sort_by": ["type": ["string", "null"], "enum": ["size", "modified", "name", nil]],
            "ascending": ["type": ["boolean", "null"]],
            "extensions": ["type": ["array", "null"], "items": ["type": "string"]],
            "folder_hint": ["type": ["string", "null"]],
            "name_contains": ["type": ["array", "null"], "items": ["type": "string"]],
            "paths": ["type": ["array", "null"], "items": ["type": "string"]],
            "modified_within_days": ["type": ["number", "null"]],
            "not_modified_for_days": ["type": ["number", "null"]],
            "group_by": ["type": ["string", "null"], "enum": ["folder", nil]],
            "limit": ["type": ["integer", "null"]],
        ],
    ]
)

public let groupDefinition = ToolDefinition(
    name: groupFilesTool,
    description: "허용된 폴더 안 파일을 비슷한 것끼리 묶어 보여준다(읽기 전용, 파일 내용은 보지 않음). "
        + "keywords를 주면 이름에 그 말이 들어간 파일을 말마다 한 묶음으로, 없으면 by에 따라 "
        + "name(이름 앞부분이 같은 파일)·kind(이미지·문서·표 등 종류) 묶음을 준다(기본 둘 다). "
        + "extensions·folder_hint는 사용자가 형식·폴더를 말했을 때만 쓴다. 이미 같은 이름 폴더에 "
        + "든 파일은 빠진다. 결과의 folder는 옮길 폴더 이름 제안이다. 결과는 사용자에게 "
        + "보이지 않으니 묶음 이름과 파일 수·예시 파일을 답에 적는다. 사용자가 옮기라고 "
        + "했을 때만 local__create_directory 후 파일마다 local__move_file로 옮기고, 옮기라는 "
        + "말이 없으면 묶음만 알리고 옮길지 물어본다. 직전에 보여준 묶음을 옮기라는 요청이면 "
        + "같은 인자로 다시 불러 경로를 얻는다.",
    schema: [
        "type": "object",
        "properties": [
            "keywords": ["type": ["array", "null"], "items": ["type": "string"]],
            "by": ["type": ["string", "null"], "enum": ["name", "kind", nil]],
            "extensions": ["type": ["array", "null"], "items": ["type": "string"]],
            "folder_hint": ["type": ["string", "null"]],
        ],
    ]
)

// MARK: - Text helpers (Python str semantics: code points, not grapheme clusters)

/// Python `<` on str: code point order. Swift's String `<` compares canonical forms instead.
func pyLess(_ a: String, _ b: String) -> Bool { a.unicodeScalars.lexicographicallyPrecedes(b.unicodeScalars) }

/// Python `s[:n]` on code points.
func prefix(_ s: String, _ n: Int) -> String { String(String.UnicodeScalarView(s.unicodeScalars.prefix(n))) }

/// Code point substring test (Python `in`).
func has(_ hay: String, _ needle: String) -> Bool { hay.range(of: needle, options: .literal) != nil }

/// Python `str.isspace` (re `\s` for str patterns): White_Space plus U+001C..U+001F.
func isSpace(_ s: Unicode.Scalar) -> Bool { s.properties.isWhitespace || (0x1C...0x1F).contains(s.value) }

/// Compare form: macOS names are often NFD, and '-', '_', '.' and spaces are one separator.
func fold(_ text: String) -> String {
    // `.caseInsensitive` folding is Unicode full case folding, like Python `casefold()`.
    let lowered = text.precomposedStringWithCanonicalMapping.folding(options: .caseInsensitive, locale: nil)
    var out = String.UnicodeScalarView()
    var gap = false
    for scalar in lowered.unicodeScalars {
        if scalar == "-" || scalar == "_" || scalar == "." || isSpace(scalar) { gap = true; continue }
        if gap, !out.isEmpty { out.append(" ") }
        gap = false
        out.append(scalar)
    }
    return String(out)
}

/// Python `str.isdigit`.
func isDigit(_ s: Unicode.Scalar?) -> Bool {
    guard let type = s?.properties.numericType else { return false }
    return type == .decimal || type == .digit
}

func regex(_ pattern: String) -> NSRegularExpression {
    // Patterns are built from escaped text, so they always compile.
    try! NSRegularExpression(pattern: pattern)
}

let digitsPattern = regex(#"\d+"#)

extension NSRegularExpression {
    func search(_ text: String) -> Bool { firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil }
    func count(_ text: String) -> Int { numberOfMatches(in: text, range: NSRange(text.startIndex..., in: text)) }
    func sub(_ template: String, _ text: String) -> String {
        stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }
}

/// Substring match, but a number at either edge must not be part of a longer number.
func keywordPattern(_ keyword: String) -> NSRegularExpression {
    let folded = fold(keyword)
    let before = isDigit(folded.unicodeScalars.first) ? #"(?<!\d)"# : ""
    let after = isDigit(folded.unicodeScalars.last) ? #"(?!\d)"# : ""
    return regex(before + NSRegularExpression.escapedPattern(for: folded) + after)
}

/// `str(Path(raw))`: collapses "//" and "." parts, drops a trailing "/".
func pyPath(_ raw: String) -> String {
    let parts = raw.split(separator: "/").filter { $0 != "." }.joined(separator: "/")
    let head = raw.hasPrefix("//") && !raw.hasPrefix("///") ? "//" : raw.hasPrefix("/") ? "/" : ""
    let joined = head + parts
    return joined.isEmpty ? "." : joined
}

// MARK: - Arguments (Python dict.get and truthiness)

func arg(_ arguments: JSON, _ key: String) -> JSON? {
    guard let value = arguments[key], !value.isNull else { return nil }
    return value
}

func truthy(_ value: JSON?) -> Bool {
    switch value {
    case nil, .null?: false
    case let .bool(b)?: b
    case let .int(i)?: i != 0
    case let .double(d)?: d != 0
    case let .string(s)?: !s.isEmpty
    case let .array(a)?: !a.isEmpty
    case let .object(o)?: !o.isEmpty
    }
}

func strings(_ value: JSON?) -> [String] {
    truthy(value) ? (value?.arrayValue ?? []).compactMap(\.stringValue) : []
}

/// `list(dict.fromkeys(items))`.
func unique(_ items: [String]) -> [String] {
    var seen = Set<String>()
    return items.filter { seen.insert($0).inserted }
}

// MARK: - Walking (never through a link)

struct Entry: Sendable {
    let root: String  // `str(root)`
    let rootName: String
    let folder: [String]  // directories below the root, in order
    let name: String
    let mtime: Double
    let size: Int

    var path: String { joinPath(root, folder + [name]) }
    var parent: String { joinPath(root, folder) }
    var dot: Int? {
        let scalars = Array(name.unicodeScalars)
        guard let i = scalars.lastIndex(of: "."), i > 0, i < scalars.count - 1 else { return nil }
        return i
    }
    var stem: String { dot.map { prefix(name, $0) } ?? name }
    var `extension`: String {
        guard let i = dot else { return "" }
        let suffix = String(String.UnicodeScalarView(name.unicodeScalars.dropFirst(i)))
        return String(suffix.lowercased().drop { $0 == "." })
    }
    var folderLabel: String { ([rootName] + folder).joined(separator: "/") }
}

func joinPath(_ root: String, _ parts: [String]) -> String {
    parts.isEmpty ? root : (root.hasSuffix("/") ? root : root + "/") + parts.joined(separator: "/")
}

struct OSFailure: Error { let code: Int32 }

/// Open root/parts one element at a time, never through a link.
///
/// Each element is opened relative to the directory already held, so a folder swapped for a
/// link after it was listed is refused instead of followed.
func openBelow(_ root: String, _ parts: [String], _ flags: Int32) throws(OSFailure) -> Int32 {
    var descriptor = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard descriptor >= 0 else { throw OSFailure(code: errno) }
    for (index, part) in parts.enumerated() {
        let last = index == parts.count - 1
        let opened = openat(descriptor, part, (last ? flags : O_RDONLY | O_DIRECTORY) | O_NOFOLLOW | O_CLOEXEC)
        let code = errno
        close(descriptor)
        guard opened >= 0 else { throw OSFailure(code: code) }
        descriptor = opened
    }
    return descriptor
}

struct Row {
    let name: String
    let isDir: Bool
    let isFile: Bool
    var mtime = 0.0
    var size = 0
}

/// (rows, more): at most `limit` children, read through a held descriptor and sorted by name.
/// A folder with more entries than the limit is never listed whole in memory.
func listDir(_ root: String, _ folder: [String], limit: Int) throws(OSFailure) -> ([Row], Bool) {
    let descriptor = try openBelow(root, folder, O_RDONLY | O_DIRECTORY)
    guard let dir = fdopendir(descriptor) else {
        let code = errno
        close(descriptor)
        throw OSFailure(code: code)
    }
    defer { closedir(dir) }
    var rows: [Row] = []
    var more = false
    var index = 0
    while let item = readdir(dir) {
        let length = Int(item.pointee.d_namlen)
        let name = withUnsafeBytes(of: item.pointee.d_name) { String(decoding: $0.prefix(length), as: UTF8.self) }
        if name == "." || name == ".." { continue }
        if index >= limit { more = true; break }
        index += 1
        var info = stat()
        guard !name.hasPrefix("."), fstatat(dirfd(dir), name, &info, AT_SYMLINK_NOFOLLOW) == 0 else {
            rows.append(Row(name: name, isDir: false, isFile: false))
            continue
        }
        let type = info.st_mode & S_IFMT
        if type == S_IFREG {
            let mtime = Double(info.st_mtimespec.tv_sec) + Double(info.st_mtimespec.tv_nsec) * 1e-9
            rows.append(Row(name: name, isDir: false, isFile: true, mtime: mtime, size: Int(info.st_size)))
        } else {
            rows.append(Row(name: name, isDir: type == S_IFDIR, isFile: false))
        }
    }
    return (rows.sorted { pyLess($0.name, $1.name) }, more)
}

/// Roots come canonical from the config; resolving here would follow a link swapped in later.
func walk(_ roots: [String], _ limits: SearchLimits) -> ([Entry], Bool) {
    var entries: [Entry] = []
    var visited = 0
    var truncated = false
    let clock = ContinuousClock()
    let started = clock.now
    var stack = roots.reversed().map { ($0, [String]()) }
    while let (root, folder) = stack.popLast() {
        if started.duration(to: clock.now) > .seconds(limits.walkSeconds) { return (entries, true) }
        guard let (rows, more) = try? listDir(root, folder, limit: limits.maxVisited - visited + 1) else { continue }
        truncated = truncated || more
        let rootName = root == "/" ? "" : String(root.split(separator: "/").last ?? "")
        for row in rows {
            visited += 1
            if visited > limits.maxVisited { return (entries, true) }
            if row.isDir {
                if excludedDirs.contains(row.name) { continue }
                if folder.count >= limits.maxDepth { truncated = true; continue }
                stack.append((root, folder + [row.name]))
            } else if row.isFile {
                entries.append(Entry(
                    root: root, rootName: rootName, folder: folder, name: row.name, mtime: row.mtime, size: row.size
                ))
            }
        }
    }
    return (entries, truncated)
}

func readHead(_ entry: Entry, _ limits: SearchLimits) throws(OSFailure) -> String {
    let descriptor = try openBelow(entry.root, entry.folder + [entry.name], O_RDONLY)
    defer { close(descriptor) }
    var info = stat()
    guard fstat(descriptor, &info) == 0 else { throw OSFailure(code: errno) }
    if info.st_mode & S_IFMT != S_IFREG { return "" }
    var buffer = [UInt8](repeating: 0, count: max(limits.contentBytes, 0))
    let count = read(descriptor, &buffer, buffer.count)
    guard count >= 0 else { throw OSFailure(code: errno) }
    return String(decoding: buffer.prefix(count), as: UTF8.self)
}

// MARK: - Filtering and dates

/// Local midnight starting that day; for `end`, midnight starting the next day (exclusive).
func day(_ value: JSON?, end: Bool) throws(ToolError) -> Double? {
    guard truthy(value), let raw = value?.stringValue else { return nil }
    // ponytail: YYYY-MM-DD and YYYYMMDD only; Python also takes ISO week dates (2026-W41-3).
    let digits = raw.utf8.count == 10 && Array(raw.utf8)[4] == UInt8(ascii: "-") && Array(raw.utf8)[7] == UInt8(ascii: "-")
        ? raw.utf8.filter { $0 != UInt8(ascii: "-") } : Array(raw.utf8)
    let leap = { (y: Int) in y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) }
    guard digits.count == 8, digits.allSatisfy({ (0x30...0x39).contains($0) }), raw.utf8.count == 8 || raw.utf8.count == 10
    else { throw .rejected("잘못된 날짜입니다: \(raw)") }
    let number = { (range: Range<Int>) in digits[range].reduce(0) { $0 * 10 + Int($1 - 0x30) } }
    let (year, month, dayOfMonth) = (number(0..<4), number(4..<6), number(6..<8))
    let lengths = [31, leap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    guard year >= 1, (1...12).contains(month), dayOfMonth >= 1, dayOfMonth <= lengths[month - 1] else {
        throw .rejected("잘못된 날짜입니다: \(raw)")
    }
    var parts = tm()
    parts.tm_year = Int32(year - 1900)
    parts.tm_mon = Int32(month - 1)
    parts.tm_mday = Int32(dayOfMonth + (end ? 1 : 0))  // mktime rolls over month and year ends
    parts.tm_isdst = -1
    return Double(mktime(&parts))
}

/// `datetime.fromtimestamp(t).strftime(...)` fields (local time, rounded to microseconds).
func localTime(_ t: Double) -> tm {
    var seconds = time_t(((t * 1e6).rounded(.toNearestOrEven) / 1e6).rounded(.down))
    var parts = tm()
    localtime_r(&seconds, &parts)
    return parts
}

func dateLabel(_ t: Double) -> String {
    let p = localTime(t)
    return String(format: "%04d-%02d-%02d", Int(p.tm_year) + 1900, Int(p.tm_mon) + 1, Int(p.tm_mday))
}

func minuteLabel(_ t: Double) -> String {
    let p = localTime(t)
    return dateLabel(t) + String(format: " %02d:%02d", Int(p.tm_hour), Int(p.tm_min))
}

/// The hint as folder words. A path under an allowed root counts as its folders below.
func folderHint(_ arguments: JSON, _ roots: [String]) -> String? {
    guard truthy(arg(arguments, "folder_hint")), var raw = arg(arguments, "folder_hint")?.stringValue else {
        return nil
    }
    if raw.hasPrefix("/") {
        let path = pyPath(raw)
        if let root = roots.first(where: { isRelative(path, to: $0) }) {
            raw = path.split(separator: "/").dropFirst(root.split(separator: "/").count).joined(separator: "/")
        }
    }
    let folded = fold(raw)
    return folded.isEmpty ? nil : folded
}

func filter(_ entries: [Entry], _ arguments: JSON, _ roots: [String]) throws(ToolError) -> [Entry] {
    let extensions = Set(
        strings(arg(arguments, "extensions")).prefix(20).filter { !$0.isEmpty }
            .map { String($0.lowercased().drop { $0 == "." }) }
    )
    let after = try day(arg(arguments, "modified_after"), end: false)
    let before = try day(arg(arguments, "modified_before"), end: true)
    if let after, let before, after >= before {
        throw .rejected("modified_after가 modified_before보다 늦습니다.")
    }
    let hint = folderHint(arguments, roots)
    return entries.filter { entry in
        (extensions.isEmpty || extensions.contains(entry.extension))
            && (after.map { entry.mtime >= $0 } ?? true)
            && (before.map { entry.mtime < $0 } ?? true)
            && (hint.map { has(fold(entry.folderLabel), $0) } ?? true)
    }
}

// MARK: - Staged search

/// What the content scan could not look at, so "nothing found" is never over-claimed.
struct Coverage {
    var cut = false
    var headOnly = 0  // text files longer than the part that is read
    var unreadable = 0
    var documents = 0
}

func score(_ text: String, _ patterns: [NSRegularExpression], all: Bool) -> Int {
    patterns.reduce(0) { $0 + (all ? $1.count(text) : ($1.search(text) ? 1 : 0)) }
}

/// (stage, matches, coverage of the content scan; empty before that stage).
func search(_ entries: [Entry], _ patterns: [NSRegularExpression], _ limits: SearchLimits)
    -> (String, [(Entry, Int)], Coverage)
{
    // With the extension: 'old.log' must find old.log.
    let byName = entries.map { ($0, score(fold($0.name), patterns, all: false)) }.filter { $0.1 > 0 }
    if !byName.isEmpty { return ("name", byName, Coverage()) }
    let byPath = entries.map { ($0, score(fold($0.folder.joined(separator: "/")), patterns, all: false)) }
        .filter { $0.1 > 0 }
    if !byPath.isEmpty { return ("path", byPath, Coverage()) }
    var coverage = Coverage(documents: entries.filter { documentExtensions.contains($0.extension) }.count)
    let clock = ContinuousClock()
    let started = clock.now
    var scanned = 0
    var byContent: [(Entry, Int)] = []
    for entry in entries {
        if !textExtensions.contains(entry.extension) || entry.size == 0 { continue }
        if scanned >= limits.contentMaxFiles || started.duration(to: clock.now) > .seconds(limits.contentSeconds) {
            coverage.cut = true
            break
        }
        scanned += 1
        guard let head = try? readHead(entry, limits) else { coverage.unreadable += 1; continue }
        coverage.headOnly += entry.size > limits.contentBytes ? 1 : 0
        let hits = score(fold(head), patterns, all: true)
        if hits > 0 { byContent.append((entry, hits)) }
    }
    return (byContent.isEmpty ? "none" : "content", byContent, coverage)
}

/// Why a result may not cover everything, in words for the model and the user.
func limitReasons(_ walkCut: Bool, _ coverage: Coverage, _ limits: SearchLimits) -> [String] {
    var reasons: [String] = []
    if walkCut { reasons.append("폴더가 크거나 깊어 일부 폴더·파일을 건너뛰었습니다") }
    if coverage.cut { reasons.append("내용 검사 파일 수·시간 한도에서 멈췄습니다") }
    if coverage.headOnly > 0 {
        reasons.append("긴 텍스트 파일 \(coverage.headOnly)개는 앞 \(limits.contentBytes / 1024) KiB만 검사했습니다")
    }
    if coverage.documents > 0 {
        reasons.append("pdf·docx 등 문서 \(coverage.documents)개는 내용을 검사하지 않았습니다(이름·폴더만)")
    }
    if coverage.unreadable > 0 { reasons.append("읽지 못한 파일이 \(coverage.unreadable)개 있습니다") }
    return reasons
}

/// Python `int` of each digit run (any Unicode decimal digits), compared like a list of ints.
func numbers(_ text: String) -> [[Int]] {
    var out: [[Int]] = []
    var current: [Int]?
    for scalar in text.unicodeScalars {
        if scalar.properties.numericType == .decimal, let value = scalar.properties.numericValue {
            current = (current ?? []) + [Int(value)]
        } else if let run = current {
            out.append(run)
            current = nil
        }
    }
    if let run = current { out.append(run) }
    return out.map { Array($0.drop { $0 == 0 }) }
}

func numbersLess(_ a: [[Int]], _ b: [[Int]]) -> Bool {
    for (x, y) in zip(a, b) where x != y {
        return x.count != y.count ? x.count < y.count : x.lexicographicallyPrecedes(y)
    }
    return a.count < b.count
}

/// Insertion-ordered grouping, like a Python dict of lists.
func grouped<Key: Hashable>(_ entries: [Entry], _ key: (Entry) -> Key?) -> [(Key, [Entry])] {
    var order: [Key] = []
    var groups: [Key: [Entry]] = [:]
    for entry in entries {
        guard let k = key(entry) else { continue }
        if groups[k] == nil { order.append(k) }
        groups[k, default: []].append(entry)
    }
    return order.map { ($0, groups[$0]!) }
}

struct SeriesKey: Hashable {
    let parent: String
    let ext: String
    let pattern: String
}

/// The largest group of files that differ only by their numbers, if it has several files.
func series(_ entries: [Entry]) -> JSON? {
    let groups = grouped(entries) {
        SeriesKey(parent: $0.parent, ext: $0.extension, pattern: digitsPattern.sub("#", fold($0.stem)))
    }
    // Python max(): the first group of the largest size.
    guard let (key, group) = groups.reduce(nil, { best, item in
        best.map { item.1.count > $0.1.count ? item : $0 } ?? item
    }), group.count >= 2, key.pattern.contains("#") else { return nil }
    let ordered = group.sorted { numbersLess(numbers($0.stem), numbers($1.stem)) }
    let newest = group.dropFirst().reduce(group[0]) { $1.mtime > $0.mtime ? $1 : $0 }
    return [
        "count": .int(group.count),
        "range": .string("\(ordered[0].stem) ~ \(ordered[ordered.count - 1].stem)"),
        "newest": .string(newest.path),
        "newest_label": .string("가장 최근 파일: \(newest.name) (\(dateLabel(newest.mtime)))"),
    ]
}

func sizeLabel(_ size: Int) -> String {
    if size < 1024 { return "\(size)B" }
    if size < 1 << 20 { return String(format: "%.0fKB", Double(size) / 1024) }
    return String(format: "%.1fMB", Double(size) / Double(1 << 20))
}

func describe(_ entry: Entry) -> String {
    "\(entry.name) · \(entry.folderLabel) · \(dateLabel(entry.mtime)) · \(sizeLabel(entry.size))"
}

/// (option, label): the folders, then extensions, that split the candidates best.
func facets(_ entries: [Entry]) -> [(String, String)] {
    var facets: [(String, String)] = []
    for (kind, values) in [
        ("폴더", entries.map(\.folderLabel)),
        ("형식", entries.filter { !$0.extension.isEmpty }.map { ".\($0.extension)" }),
    ] {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for value in values {
            if counts[value] == nil { order.append(value) }
            counts[value, default: 0] += 1
        }
        // A facet every candidate shares cannot narrow anything.
        for value in order.sorted(by: { counts[$0]! > counts[$1]! }) where counts[value]! < entries.count {
            facets.append(("\(kind): \(value)", "\(kind): \(value) (\(counts[value]!)개)"))
        }
    }
    return Array(facets.prefix(maxFacets))
}

/// Keep the candidates whose beginning mentions one of the words; best match first.
///
/// Only text files can be checked. A candidate that cannot be (a pdf, an unreadable file) is
/// left out and counted, so the answer says what it could not look into.
func narrowByContent(_ ranked: [Entry], _ words: [NSRegularExpression], _ start: Coverage, _ limits: SearchLimits)
    -> ([Entry], Coverage)
{
    var coverage = start
    let clock = ContinuousClock()
    let started = clock.now
    var scanned = 0
    var kept: [(Entry, Int)] = []
    for entry in ranked {
        if !textExtensions.contains(entry.extension) {
            coverage.documents += documentExtensions.contains(entry.extension) ? 1 : 0
            continue
        }
        if scanned >= limits.contentMaxFiles || started.duration(to: clock.now) > .seconds(limits.contentSeconds) {
            coverage.cut = true
            break
        }
        scanned += 1
        guard let head = try? readHead(entry, limits) else { coverage.unreadable += 1; continue }
        coverage.headOnly += entry.size > limits.contentBytes ? 1 : 0
        let hits = score(fold(head), words, all: true)
        if hits > 0 { kept.append((entry, hits)) }
    }
    return (rank(kept), coverage)
}

/// Best match first; newer files break ties, then the name.
func rank(_ matches: [(Entry, Int)]) -> [Entry] {
    matches.sorted { a, b in
        if a.1 != b.1 { return a.1 > b.1 }
        if a.0.mtime != b.0.mtime { return a.0.mtime > b.0.mtime }
        return pyLess(a.0.name, b.0.name)
    }.map(\.0)
}

func rootPaths(_ roots: [URL]) -> [String] { roots.map { pyPath($0.path) } }

// MARK: - Tools

public func findFiles(_ arguments: JSON, roots: [URL], limits: SearchLimits = SearchLimits()) throws(ToolError)
    -> ToolResult
{
    try validateArguments(findFilesDefinition, arguments)
    let roots = rootPaths(roots)
    let keywords = Array(unique(strings(arg(arguments, "keywords")).map { fold(prefix($0, 100)) }.filter { !$0.isEmpty })
        .prefix(maxKeywords))
    if keywords.isEmpty { throw .rejected("검색어가 비어 있습니다.") }
    let patterns = keywords.map(keywordPattern)
    let (entries, walkCut) = walk(roots, limits)
    let scope = try filter(entries, arguments, roots)
    var (stage, matches, coverage) = search(scope, patterns, limits)
    let filtered = filterKeys.contains { truthy(arg(arguments, $0)) }
    var ranked = rank(matches)
    let contentWords = unique(strings(arg(arguments, "content_keywords")).prefix(maxKeywords).map { fold(prefix($0, 100)) })
        .filter { !$0.isEmpty }.map(keywordPattern)
    if !contentWords.isEmpty, !ranked.isEmpty {
        (ranked, coverage) = narrowByContent(ranked, contentWords, coverage, limits)
        stage += "+content"
    }
    let count = ranked.count
    let reasons = limitReasons(walkCut, coverage, limits)
    var decision = count == 0 ? "none" : count == 1 ? "single" : count <= maxShow ? "choose" : "narrow"
    if decision == "single", !reasons.isEmpty {
        // One hit among files that were not fully looked at is not "the only one": confirm it.
        decision = "choose"
    }
    let truncated = walkCut || coverage.cut
    var response: JSON = [
        "stage": .string(stage), "decision": .string(decision), "total": .int(count),
        "truncated": .bool(truncated), "complete": .bool(reasons.isEmpty),
    ]
    var notes: [String] = []
    if !reasons.isEmpty {
        response.set("limits", .array(reasons.map(JSON.string)))  // for the app's own wording
        notes.append("검사 범위의 한계: " + reasons.joined(separator: "; ") + ".")
        if decision == "none" { notes.append("없다고 단정하지 말고, 파일명·폴더 같은 단서를 사용자에게 물으세요.") }
    }
    if filtered, decision == "none" {
        // Never widen on our own: a condition the user named must not be dropped silently.
        notes.append(
            "형식·폴더·날짜 조건을 적용했을 때 결과가 없습니다. 사용자가 말한 조건이면 풀지 말고 "
                + "그대로 알리세요. 사용자가 말하지 않은 추측 조건이면 빼고 다시 호출하세요."
        )
    }
    if !notes.isEmpty { response.set("note", .string(notes.joined(separator: " "))) }
    if decision == "single" || decision == "choose" {
        response.set("candidates", .array(ranked.map { ["path": .string($0.path), "label": .string(describe($0))] }))
    } else if decision == "narrow" {
        if let series = series(ranked), let total = series["count"]?.intValue, total > maxShow {
            response.set("series", series)
        }
        response.set("narrow_options", .array(facets(ranked).map { ["option": .string($0.0), "label": .string($0.1)] }))
    }
    return ToolResult(ok: true, text: response.text, structured: response, truncated: truncated)
}

public func listFiles(_ arguments: JSON, roots: [URL], limits: SearchLimits = SearchLimits()) throws(ToolError)
    -> ToolResult
{
    try validateArguments(listDefinition, arguments)
    let within = arg(arguments, "modified_within_days")?.doubleValue
    let idle = arg(arguments, "not_modified_for_days")?.doubleValue
    if [within, idle].contains(where: { ($0 ?? 0) < 0 }) { throw .rejected("일 수는 0 이상이어야 합니다.") }
    let roots = rootPaths(roots)
    var (entries, truncated) = walk(roots, limits)
    entries = try filter(entries, arguments, roots)
    let words = strings(arg(arguments, "name_contains")).prefix(maxKeywords).map { fold(prefix($0, 100)) }
        .filter { !$0.isEmpty }
    let rawPaths = strings(arg(arguments, "paths"))
    if rawPaths.count > listMax { throw .rejected("paths는 \(listMax)개까지입니다.") }
    // Byte keys: Python compares paths by code point, Swift Strings by canonical equivalence.
    let paths = Set(rawPaths.map { Array(pyPath($0).utf8) })
    let now = Date().timeIntervalSince1970
    entries = entries.filter { entry in
        (paths.isEmpty || paths.contains(Array(entry.path.utf8)))
            && (words.isEmpty || words.contains { has(fold(entry.name), $0) })
            && (within.map { entry.mtime >= now - $0 * 86400 } ?? true)
            && (idle.map { entry.mtime < now - $0 * 86400 } ?? true)
    }
    let asked = arg(arguments, "limit")?.doubleValue.map { Int(max(-1, min($0, Double(listMax)))) } ?? 0
    let limit = max(1, min(asked == 0 ? 20 : asked, listMax))
    let descending = !truthy(arg(arguments, "ascending"))
    var response: JSON = ["total": .int(entries.count), "truncated": .bool(truncated)]
    if arg(arguments, "group_by")?.stringValue == "folder" {
        var order: [String] = []
        var totals: [String: (Int, Int)] = [:]
        for entry in entries {
            guard let top = entry.folder.first else { continue }
            // Two allowed roots may each hold a folder of the same name, and roots may share a
            // name too, so the full path.
            let label = roots.count > 1 ? joinPath(entry.root, [top]) : top
            if totals[label] == nil { order.append(label) }
            let total = totals[label] ?? (0, 0)
            totals[label] = (total.0 + entry.size, total.1 + 1)
        }
        var folders = order.sorted { a, b in
            totals[a]!.0 != totals[b]!.0 ? totals[a]!.0 > totals[b]!.0 : pyLess(a, b)
        }
        if !descending { folders.reverse() }
        response.set("folders", .array(folders.prefix(limit).map { name in
            let (size, count) = totals[name]!
            return ["folder": .string(name), "size_bytes": .int(size), "size": .string(sizeLabel(size)), "files": .int(count)]
        }))
    } else {
        let key = arg(arguments, "sort_by")?.stringValue ?? "modified"
        let ordered: [Entry]
        switch key {
        case "name": ordered = entries.sorted { descending ? pyLess($0.name, $1.name) : pyLess($1.name, $0.name) }
        case "size": ordered = entries.sorted { descending ? $0.size > $1.size : $0.size < $1.size }
        default: ordered = entries.sorted { descending ? $0.mtime > $1.mtime : $0.mtime < $1.mtime }
        }
        let files: [JSON] = ordered.prefix(limit).map { entry in
            [
                "name": .string(entry.name), "path": .string(entry.path), "size_bytes": .int(entry.size),
                "size": .string(sizeLabel(entry.size)), "modified": .string(minuteLabel(entry.mtime)),
            ]
        }
        response.set("files", .array(files))
        response.set("shown", .int(files.count))
    }
    return ToolResult(ok: true, text: response.text, structured: response, truncated: truncated)
}

/// First word of the name that is not a number: IMG_0001 -> img, 보고서_최종 -> 보고서.
///
/// ponytail: first-word heuristic; 'final report' and 'final draft' land together.
func nameKey(_ entry: Entry) -> String? {
    for part in fold(entry.stem).split(separator: " ") {
        let word = digitsPattern.sub("", String(part))
        if word.unicodeScalars.count >= 2 { return word }
    }
    return nil
}

public func groupFiles(_ arguments: JSON, roots: [URL], limits: SearchLimits = SearchLimits()) throws(ToolError)
    -> ToolResult
{
    try validateArguments(groupDefinition, arguments)
    let roots = rootPaths(roots)
    var (entries, truncated) = walk(roots, limits)
    entries = try filter(entries, arguments, roots)
    let keywords = Array(unique(strings(arg(arguments, "keywords")).map { fold(prefix($0, 100)) }.filter { !$0.isEmpty })
        .prefix(maxKeywords))
    var groups: [(String, [(String, [Entry])])] = []
    if !keywords.isEmpty {
        groups.append(("keyword", keywords.map { word in
            let pattern = keywordPattern(word)
            return (word, entries.filter { pattern.search(fold($0.name)) })
        }))
    } else {
        let by = arg(arguments, "by")?.stringValue
        if by == nil || by == "name" {
            groups.append(("name", grouped(entries, nameKey).filter { $0.1.count >= 2 }))
        }
        if by == nil || by == "kind" {
            groups.append(("kind", grouped(entries) { entry in kinds.first { $0.1.contains(entry.extension) }?.0 }))
        }
    }
    var found: [(count: Int, item: JSON)] = []
    for (by, members) in groups {
        for (label, files) in members {
            // A file already in a folder of the group's name is organized already.
            let kept = files.filter { $0.folder.last.map { fold($0) != label } ?? true }.sorted { a, b in
                a.folderLabel != b.folderLabel ? pyLess(a.folderLabel, b.folderLabel) : pyLess(a.name, b.name)
            }
            if kept.isEmpty { continue }
            found.append((kept.count, [
                "by": .string(by), "label": .string(label), "folder": .string(label), "count": .int(kept.count),
                "files": .array(kept.prefix(limits.groupShow).map { .string($0.path) }),
            ]))
        }
    }
    found.sort { $0.count > $1.count }
    let response: JSON = [
        "groups": .array(found.prefix(maxGroups).map(\.item)),
        "more_groups": .int(max(0, found.count - maxGroups)),
        "truncated": .bool(truncated),
    ]
    return ToolResult(ok: true, text: response.text, structured: response, truncated: truncated)
}

/// (question, kind, options, labels) when the user must pick or narrow; else nil.
public func clarificationSpec(_ response: JSON)
    -> (question: String, kind: String, options: [String], labels: [String])?
{
    let decision = response["decision"]?.stringValue
    let partial = response["complete"] == .bool(false)
    let text = { (value: JSON?) in value?.stringValue ?? value?.dumps() ?? "None" }
    if decision == "choose" {
        let items = response["candidates"]?.arrayValue ?? []
        return (
            "어떤 파일인가요? 하나를 선택해 주세요.\(partial ? " (일부만 검사한 결과예요)" : "")", "file_candidate",
            items.map { text($0["path"]) }, items.map { text($0["label"]) }
        )
    }
    guard decision == "narrow" else { return nil }
    var options: [String] = []
    var labels: [String] = []
    let note = partial ? " (일부만 검사한 결과라 실제 후보는 더 많을 수 있어요)" : ""
    let head: String
    if let series = response["series"], truthy(series) {
        options.append(text(series["newest"]))
        labels.append(text(series["newest_label"]))
        head = "번호만 다른 파일이 \(text(series["count"]))개 있어요 (\(text(series["range"]))). "
            + "원하는 번호나 날짜를 입력하거나 버튼을 눌러 주세요.\(note)"
    } else {
        head = "후보가 \(text(response["total"]))개로 많아요. 폴더, 날짜, 형식, 파일 내용의 단서를 "
            + "입력하거나 버튼으로 범위를 좁혀 주세요.\(note)"
    }
    for item in response["narrow_options"]?.arrayValue ?? [] {
        options.append(text(item["option"]))
        labels.append(text(item["label"]))
    }
    return (head, "narrow_search", options, labels)
}
