import CryptoKit
import Foundation
import PDFKit
import SymbioCore

/// Python `conversion.py`: file conversion (approved write) and document reading (read-only).
///
/// Converters are fixed macOS tools and the standard library; the model only names the source
/// file and the target format. Conversions run in a temporary folder before approval, so the
/// approval shows the real result, and the output is always a new file next to the source.

public let convertTool = "local__convert_file"
public let readDocumentTool = "local__read_document"
public let maxSourceBytes = 20 * 1024 * 1024
public let maxOutputBytes = 50 * 1024 * 1024
public let timeoutSeconds = 60.0
public let previewChars = 200

let textutil = "/usr/bin/textutil", sips = "/usr/bin/sips", cupsfilter = "/usr/sbin/cupsfilter"
/// Extension aliases -> canonical format name.
public let aliases = ["jpg": "jpeg", "htm": "html", "tif": "tiff", "heif": "heic", "markdown": "md"]
public let textDocuments: Set = ["txt", "md"]
public let textutilInput: Set = ["txt", "md", "rtf", "html", "doc", "docx", "odt"]
public let textutilOutput: Set = ["txt", "rtf", "html", "doc", "docx", "odt"]
public let images: Set = ["png", "jpeg", "heic", "tiff", "gif", "bmp"]
public let readableDocuments: Set = ["pdf", "rtf", "html", "doc", "docx", "odt"]
public let textOutputs: Set = ["txt", "rtf", "html", "csv", "json"]
/// The extension written for a format, default ".<format>".
func extensionFor(_ format: String) -> String { format == "jpeg" ? ".jpg" : "." + format }
public let targetFormats = textutilOutput.union(images).union(["pdf", "csv", "json"]).sorted()
/// Leading bytes for formats that have them; a mismatch with the extension is refused.
let signatures: [String: [[UInt8]]] = [
    "pdf": [Array("%PDF".utf8)],
    "png": [[0x89, 0x50, 0x4E, 0x47]],
    "jpeg": [[0xFF, 0xD8, 0xFF]],
    "gif": [Array("GIF8".utf8)],
    "bmp": [Array("BM".utf8)],
    "tiff": [[0x49, 0x49, 0x2A, 0x00], [0x4D, 0x4D, 0x00, 0x2A]],
    "docx": [[0x50, 0x4B, 0x03, 0x04]],
    "odt": [[0x50, 0x4B, 0x03, 0x04]],
    "doc": [[0xD0, 0xCF, 0x11, 0xE0]],
    "rtf": [Array("{\\rtf".utf8)],
    "hwpx": [[0x50, 0x4B, 0x03, 0x04]],
]

public let convertDefinition = ToolDefinition(
    name: convertTool,
    description: "파일 형식 변환. 원본은 그대로 두고 같은 폴더에 새 파일을 만든다(덮어쓰지 않음). "
        + "문서 txt·md·rtf·html·doc·docx·odt 사이 변환, txt·md→pdf(서식 없는 글), pdf→txt, "
        + "이미지 png·jpeg(jpg)·heic·tiff·gif·bmp 사이 변환(아이폰 HEIC→JPG·PNG 포함), "
        + "hwpx(한글)→pdf(서식 없는 글), csv↔json. 사용자 승인 후 실행된다.",
    schema: [
        "type": "object",
        "properties": [
            "source": ["type": "string", "minLength": 1],
            // The aliases ("jpg", "tif"...) are listed too: a provider rejects any value outside
            // the enum before this code could map it, and "jpg" is what people say.
            "target_format": [
                "type": "string",
                "enum": .array(Set(targetFormats).union(aliases.keys).sorted().map { .string($0) }),
            ],
            "output": ["type": "string", "minLength": 1],
        ],
        "required": ["source", "target_format"],
        "additionalProperties": false,
    ]
)

public let readDocumentDefinition = ToolDefinition(
    name: readDocumentTool,
    description: "pdf·docx·doc·rtf·odt·html 문서의 글자를 읽어 텍스트로 돌려준다. 파일을 만들거나 "
        + "바꾸지 않는다. 일반 텍스트 파일(txt, md, csv)은 filesystem__read_text_file을 쓴다.",
    schema: [
        "type": "object",
        "properties": ["path": ["type": "string", "minLength": 1]],
        "required": ["path"],
        "additionalProperties": false,
    ]
)

public struct PreparedConversion: Equatable, Sendable {
    public let source: String
    public let output: String
    public let targetFormat: String
    public let sourceState: String
    public let data: Data

    public var preview: String {
        guard textOutputs.contains(targetFormat) else { return "" }
        return String(String(decoding: data, as: UTF8.self).unicodeScalars.prefix(previewChars))
    }

    public var previewTruncated: Bool { textOutputs.contains(targetFormat) && data.count > previewChars }
}

/// Python `PurePath.suffix` / `stem`: "" suffix for ".bashrc" and "a.".
func splitSuffix(_ name: String) -> (stem: String, suffix: String) {
    guard let dot = name.lastIndex(of: "."), dot != name.startIndex, dot != name.index(before: name.endIndex)
    else { return (name, "") }
    return (String(name[..<dot]), String(name[dot...]))
}

public func formatOf(_ path: URL) -> String {
    let ext = String(splitSuffix(path.lastPathComponent).suffix.lowercased().dropFirst())
    return aliases[ext] ?? ext
}

private func exists(_ path: String) -> Bool {
    var info = stat()
    return lstat(path, &info) == 0
}

/// Bytes of a regular file and its identity (device, inode, content hash).
func readSource(_ path: URL, maxSource: Int = maxSourceBytes) throws(ToolError) -> (Data, String) {
    let descriptor = open(path.path, O_RDONLY | O_NOFOLLOW)
    if descriptor < 0 { throw .rejected("원본 파일을 열 수 없습니다.") }
    defer { close(descriptor) }
    var info = stat()
    guard fstat(descriptor, &info) == 0 else { throw .rejected("원본 파일을 열 수 없습니다.") }
    if info.st_mode & S_IFMT != S_IFREG { throw .rejected("일반 파일만 변환할 수 있습니다.") }
    let tooBig = ToolError.rejected("원본 파일은 최대 20 MiB까지 변환할 수 있습니다.")
    if Int(info.st_size) > maxSource { throw tooBig }
    var data = Data()
    var chunk = [UInt8](repeating: 0, count: 1024 * 1024)
    // Bounded even if the file grows after the size check.
    while true {
        let count = read(descriptor, &chunk, min(chunk.count, maxSource + 1 - data.count))
        if count < 0, errno == EINTR { continue }
        if count < 0 { throw .rejected("원본 파일을 읽을 수 없습니다.") }
        if count == 0 { break }
        data.append(contentsOf: chunk[..<count])
        if data.count > maxSource { throw tooBig }
    }
    let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    return (data, "\(info.st_dev):\(info.st_ino):\(digest)")
}

func checkSignature(_ data: Data, _ format: String) throws(ToolError) {
    let bytes = [UInt8](data.prefix(12))
    func at(_ range: Range<Int>) -> String { String(decoding: bytes.dropFirst(range.lowerBound).prefix(range.count), as: UTF8.self) }
    if format == "heic" {
        if at(4..<8) != "ftyp" || !["heic", "heix", "mif1", "msf1"].contains(at(8..<12)) {
            throw .rejected("확장자와 실제 파일 형식이 다릅니다.")
        }
        return
    }
    if let options = signatures[format], !options.contains(where: { bytes.starts(with: $0) }) {
        throw .rejected("확장자와 실제 파일 형식이 다릅니다.")
    }
}

/// Fixed converter binaries only, no shell, bounded time.
public func runConverter(_ command: [String]) throws(ToolError) -> Data {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: command[0])
    process.arguments = Array(command.dropFirst())
    let stdout = Pipe()
    process.standardOutput = stdout
    process.standardError = FileHandle.nullDevice
    process.standardInput = FileHandle.nullDevice
    do { try process.run() } catch { throw .rejected("이 컴퓨터에서 변환 도구를 실행할 수 없습니다.") }
    let started = Date()
    let pid = process.processIdentifier
    let killer = DispatchWorkItem { kill(pid, SIGKILL) }
    DispatchQueue.global().asyncAfter(deadline: .now() + timeoutSeconds, execute: killer)
    let output = stdout.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    killer.cancel()
    if Date().timeIntervalSince(started) >= timeoutSeconds {
        throw .rejected("변환 시간이 60초를 넘어 중단했습니다.")
    }
    if process.terminationReason != .exit || process.terminationStatus != 0 {
        throw .rejected("변환에 실패했습니다.")
    }
    return output
}

func pdfText(_ path: URL) throws(ToolError) -> String {
    guard let document = PDFDocument(url: path) else {
        throw .rejected("PDF를 열 수 없습니다. 손상되었거나 암호가 걸려 있습니다.")
    }
    return document.string ?? ""
}

/// Python `bytes.decode("utf-8-sig")`.
func decode(_ data: Data, _ kind: String) throws(ToolError) -> String {
    var bytes = [UInt8](data)
    if bytes.starts(with: [0xEF, 0xBB, 0xBF]) { bytes.removeFirst(3) }
    guard let text = String(validating: bytes, as: UTF8.self) else {
        throw .rejected("\(kind)이 UTF-8 텍스트가 아닙니다.")
    }
    return text
}

/// Python `csv.reader(io.StringIO(text), strict=True)` with the default excel dialect.
func csvRows(_ text: String) throws(ToolError) -> [[String]] {
    enum State { case startRecord, startField, inField, inQuoted, quoteInQuoted, eatCRNL }
    let bad = ToolError.rejected("CSV 형식이 올바르지 않습니다 (따옴표 등).")
    var rows: [[String]] = [], row: [String] = [], field = "", state = State.startRecord
    func save() { row.append(field); field = "" }
    // nil is the end of a line (StringIO splits lines on "\n" only).
    func step(_ c: Unicode.Scalar?) throws(ToolError) {
        let newline = c == "\n" || c == "\r" || c == nil
        switch state {
        case .startRecord, .startField:
            if state == .startRecord {
                if c == nil { return }
                if newline { state = .eatCRNL; return }
            }
            if newline { save(); state = c == nil ? .startRecord : .eatCRNL }
            else if c == "\"" { state = .inQuoted }
            else if c == "," { save(); state = .startField }
            else { try add(c!); state = .inField }
        case .inField:
            if newline { save(); state = c == nil ? .startRecord : .eatCRNL }
            else if c == "," { save(); state = .startField }
            else { try add(c!) }
        case .inQuoted:
            if c == "\"" { state = .quoteInQuoted } else if let c { try add(c) }
        case .quoteInQuoted:
            if c == "\"" { try add("\""); state = .inQuoted }
            else if c == "," { save(); state = .startField }
            else if newline { save(); state = c == nil ? .startRecord : .eatCRNL }
            else { throw bad }  // strict: ',' expected after '"'
        case .eatCRNL:
            if c == nil { state = .startRecord } else if !newline { throw bad }
        }
    }
    func add(_ c: Unicode.Scalar) throws(ToolError) {
        if field.unicodeScalars.count >= 131_072 { throw bad }  // csv.field_size_limit()
        field.unicodeScalars.append(c)
    }
    func endLine() throws(ToolError) {
        try step(nil)
        if state == .startRecord { rows.append(row); row = [] }
    }
    var pending = false
    for c in text.unicodeScalars {
        try step(c)
        pending = true
        if c == "\n" { try endLine(); pending = false }
    }
    if pending { try endLine() }
    if state == .inQuoted { throw bad }  // strict: unexpected end of data
    return rows
}

/// A header row plus rows of the same width, as an array of string-valued objects.
///
/// Anything that would silently drop or shift a value is refused instead: an empty or
/// duplicate header, a row with a different number of cells, or broken quoting.
func csvToJSON(_ data: Data) throws(ToolError) -> Data {
    let rows = try csvRows(try decode(data, "CSV")).filter { !$0.isEmpty }
    guard let header = rows.first else { throw .rejected("CSV가 비어 있습니다.") }
    if header.contains(where: { $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
        || Set(header).count != header.count
    {
        throw .rejected("CSV 헤더에 빈 이름이나 중복된 이름이 있습니다.")
    }
    for (number, row) in zip(2..., rows.dropFirst()) where row.count != header.count {
        throw .rejected("CSV \(number)번째 행의 열 수가 헤더와 다릅니다.")
    }
    let records = JSON.array(rows.dropFirst().map { row in .object(zip(header, row).map { ($0, .string($1)) }) })
    return Data(records.dumps(JSONDumpOptions(ensureASCII: false, indent: 2)).utf8)
}

/// JSON scalars as CSV text: null is empty, numbers and booleans keep their JSON form.
func csvCell(_ value: JSON) -> String {
    switch value {
    case .null: ""
    case let .string(s): s
    default: value.dumps()
    }
}

/// Python `csv.writer` (QUOTE_MINIMAL, lineterminator "\n") for one row.
func csvLine(_ fields: [String]) -> String {
    if fields == [""] { return "\"\"\n" }
    return fields.map { field in
        field.unicodeScalars.contains { $0 == "," || $0 == "\"" || $0 == "\r" || $0 == "\n" }
            ? "\"" + field.replacingOccurrences(of: "\"", with: "\"\"") + "\"" : field
    }.joined(separator: ",") + "\n"
}

func jsonToCSV(_ data: Data) throws(ToolError) -> Data {
    let text = try decode(data, "JSON")
    let parsed: JSON
    do { parsed = try JSON.parse(text) } catch { throw .rejected("JSON을 읽을 수 없습니다.") }
    guard let rows = parsed.arrayValue?.compactMap(\.objectValue), rows.count == parsed.arrayValue?.count else {
        throw .rejected("CSV로 바꿀 JSON은 객체들의 배열이어야 합니다.")
    }
    if rows.contains(where: { $0.contains { $0.1.arrayValue != nil || $0.1.objectValue != nil } }) {
        throw .rejected("중첩된 값이 있는 JSON은 CSV로 바꿀 수 없습니다.")
    }
    var header: [String] = []
    for row in rows { for (key, _) in row where !header.contains(key) { header.append(key) } }
    var out = csvLine(header)
    for row in rows {
        out += csvLine(header.map { key in row.last { $0.0 == key }.map { csvCell($0.1) } ?? "" })
    }
    return Data(out.utf8)
}

public func supported(_ sourceFormat: String, _ targetFormat: String) -> Bool {
    if sourceFormat == targetFormat { return false }
    if Set([sourceFormat, targetFormat]) == ["csv", "json"] { return true }
    return (textutilInput.contains(sourceFormat) && textutilOutput.contains(targetFormat))
        || (textDocuments.contains(sourceFormat) && targetFormat == "pdf")
        || (sourceFormat == "pdf" && targetFormat == "txt")
        || (sourceFormat == "hwpx" && targetFormat == "pdf")
        || (images.contains(sourceFormat) && images.contains(targetFormat))
}

/// Convert in a private temporary folder and return the output bytes.
/// `maxOutput` and `run` exist for tests (Python patches the module constant and `_run`).
public func convert(
    _ source: URL, data: Data, sourceFormat: String, targetFormat: String,
    maxOutput: Int = maxOutputBytes,
    run: ([String]) throws(ToolError) -> Data = runConverter
) throws(ToolError) -> Data {
    if sourceFormat == "csv", targetFormat == "json" { return try checkedOutput(try csvToJSON(data), maxOutput) }
    if sourceFormat == "json", targetFormat == "csv" { return try checkedOutput(try jsonToCSV(data), maxOutput) }
    var template = Array((NSTemporaryDirectory() + "voice-action-convert-XXXXXX").utf8CString)
    guard let made = mkdtemp(&template) else { throw .rejected("변환 결과 파일을 읽을 수 없습니다.") }
    let folder = URL(fileURLWithPath: String(cString: made))
    defer { try? FileManager.default.removeItem(at: folder) }
    // Work on a private copy, so a source swapped mid-run can't change the input.
    let copy = folder.appendingPathComponent("source.\(sourceFormat)")
    let input = sourceFormat == "hwpx" ? try hwpxText(data, maxOutput: maxOutput) : data
    do { try input.write(to: copy) } catch { throw .rejected("변환 결과 파일을 읽을 수 없습니다.") }
    let out = folder.appendingPathComponent("output\(extensionFor(targetFormat))")
    let output: Data
    if sourceFormat == "pdf" {
        output = Data(try pdfText(copy).utf8)
    } else if targetFormat == "pdf" {
        output = try run([cupsfilter, "-m", "application/pdf", "-i", "text/plain", copy.path])
    } else if images.contains(sourceFormat) {
        _ = try run([sips, "-s", "format", targetFormat, copy.path, "--out", out.path])
        output = try readArtifact(out, maxOutput)
    } else {
        let textutilFormat = sourceFormat == "md" ? ["-format", "txt"] : []
        _ = try run([textutil] + textutilFormat + ["-convert", targetFormat, "-output", out.path, copy.path])
        output = try readArtifact(out, maxOutput)
    }
    return try checkedOutput(output, maxOutput)
}

/// A converter's result file. Exit status 0 alone is not proof it exists.
func readArtifact(_ path: URL, _ maxOutput: Int) throws(ToolError) -> Data {
    var info = stat()
    if lstat(path.path, &info) != 0 {
        throw errno == ENOENT ? .rejected("변환 결과 파일이 만들어지지 않았습니다.")
            : .rejected("변환 결과 파일을 읽을 수 없습니다.")
    }
    if info.st_mode & S_IFMT != S_IFREG { throw .rejected("변환 결과 파일이 만들어지지 않았습니다.") }
    if Int(info.st_size) > maxOutput { throw .rejected("변환 결과가 50 MiB를 넘습니다.") }
    do { return try Data(contentsOf: path) } catch { throw .rejected("변환 결과 파일을 읽을 수 없습니다.") }
}

func checkedOutput(_ output: Data, _ maxOutput: Int) throws(ToolError) -> Data {
    if output.isEmpty { throw .rejected("변환 결과가 비어 있습니다.") }
    if output.count > maxOutput { throw .rejected("변환 결과가 50 MiB를 넘습니다.") }
    return output
}

/// Same folder and name, new extension; never an existing file ("이름 (1).pdf").
public func defaultOutput(_ source: URL, targetFormat: String) -> URL {
    let ext = extensionFor(targetFormat)
    let stem = splitSuffix(source.lastPathComponent).stem
    let folder = source.deletingLastPathComponent()
    var candidate = folder.appendingPathComponent(stem + ext)
    var index = 1
    while exists(candidate.path) {
        candidate = folder.appendingPathComponent("\(stem) (\(index))\(ext)")
        index += 1
    }
    return candidate
}

public func prepareConversion(
    _ arguments: JSON, roots: [URL], maxSource: Int = maxSourceBytes, maxOutput: Int = maxOutputBytes
) throws(ToolError) -> PreparedConversion {
    try validateArguments(convertDefinition, arguments)
    let source = try inside(arguments["source"], roots: roots, exists: true)
    let requested = arguments["target_format"]?.stringValue ?? ""
    let sourceFormat = formatOf(source), targetFormat = aliases[requested] ?? requested
    if !supported(sourceFormat, targetFormat) {
        let shown = sourceFormat.isEmpty ? "확장자 없음" : sourceFormat
        throw .rejected("지원하지 않는 변환입니다: \(shown) → \(targetFormat)")
    }
    let output: URL
    if let raw = arguments["output"] {
        output = try inside(raw, roots: roots, exists: false)
        if exists(output.path) { throw .rejected("출력 파일이 이미 있습니다. 덮어쓰지 않습니다.") }
        if formatOf(output) != targetFormat { throw .rejected("출력 파일 확장자가 변환 형식과 다릅니다.") }
    } else {
        output = defaultOutput(source, targetFormat: targetFormat)
        _ = try inside(.string(output.path), roots: roots, exists: false)
    }
    let (data, state) = try readSource(source, maxSource: maxSource)
    try checkSignature(data, sourceFormat)
    let converted = try convert(source, data: data, sourceFormat: sourceFormat, targetFormat: targetFormat,
                                maxOutput: maxOutput)
    return PreparedConversion(source: source.path, output: output.path, targetFormat: targetFormat,
                              sourceState: state, data: converted)
}

/// True if the source is no longer the approved file, a path left the allowed folders
/// (e.g. a parent swapped for a symlink while waiting), or the output name got taken.
public func changedSince(source: String, state: String, output: String, roots: [URL]) -> Bool {
    let current: String
    do {
        _ = try inside(.string(source), roots: roots, exists: true)
        _ = try inside(.string(output), roots: roots, exists: false)
        current = try readSource(URL(fileURLWithPath: source)).1
    } catch {
        return true
    }
    return current != state || exists(output)
}

/// Create the output only if nothing is there yet, durably, inside the allowed folders.
///
/// The boundary is checked here, at the write itself, not only when the conversion was
/// prepared. A partial write removes its own file, so a failure never leaves an orphan.
/// `writer` exists for tests (Python patches `os.write`).
public func writeNewFile(
    _ raw: String, data: Data, roots: [URL],
    writer: (Int32, UnsafeRawPointer?, Int) -> Int = Darwin.write
) throws(ToolError) {
    let path = try inside(.string(raw), roots: roots, exists: false).path
    let descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o644)
    if descriptor < 0 {
        if errno == EEXIST { throw .rejected("출력 파일이 승인 대기 중에 생겼습니다. 덮어쓰지 않습니다.") }
        throw .other("변환 파일을 만들 수 없습니다: \(String(cString: strerror(errno)))")
    }
    var created = stat()
    fstat(descriptor, &created)
    let failure: Int32? = data.withUnsafeBytes { buffer in
        var offset = 0
        while offset < buffer.count {
            let count = writer(descriptor, buffer.baseAddress! + offset, buffer.count - offset)
            if count < 0, errno == EINTR { continue }
            if count < 0 { return errno }
            offset += count
        }
        return fsync(descriptor) == 0 ? nil : errno
    }
    close(descriptor)
    if let failure {
        // Remove only the file this call created, not anything put there since.
        var current = stat()
        if lstat(path, &current) == 0, current.st_dev == created.st_dev, current.st_ino == created.st_ino {
            unlink(path)
        }
        throw .other("변환 파일을 쓰지 못했습니다: \(String(cString: strerror(failure)))")
    }
}

public func readDocument(_ arguments: JSON, roots: [URL], limit: Int) throws(ToolError) -> ToolResult {
    try validateArguments(readDocumentDefinition, arguments)
    let path = try inside(arguments["path"], roots: roots, exists: true)
    let format = formatOf(path)
    if !readableDocuments.contains(format) {
        throw .rejected("읽을 수 있는 문서 형식이 아닙니다. 텍스트 파일은 filesystem__read_text_file을 쓰세요.")
    }
    let (data, _) = try readSource(path)
    try checkSignature(data, format)
    let text = String(decoding: try convert(path, data: data, sourceFormat: format, targetFormat: "txt"),
                      as: UTF8.self)
    let truncated = text.unicodeScalars.count > limit
    let shown = String(text.unicodeScalars.prefix(limit))
    return ToolResult(ok: true, text: shown + (truncated ? truncationMarker : ""), truncated: truncated)
}
