import Compression
import Foundation
import SymbioCore

/// Hangul (한글) HWPX: a zip of OWPML XML. Body text is in Contents/section<N>.xml.
let hwpxParagraph = "http://www.hancom.co.kr/hwpml/2011/paragraph"

private let unreadable = ToolError.rejected("한글(hwpx) 파일을 읽을 수 없습니다.")

/// Python `_hwpx_text`: paragraph text of every section, in order; tables come out one cell per line.
func hwpxText(_ data: Data, maxOutput: Int) throws(ToolError) -> Data {
    let archive = try ZipArchive(Array(data))
    guard let mimetype = archive.entries.last(where: { $0.name == "mimetype" }) else { throw unreadable }
    if mimetype.size > 64 { throw .rejected("확장자와 실제 파일 형식이 다릅니다.") }
    if try archive.read(mimetype) != Array("application/hwp+zip".utf8) {
        throw .rejected("확장자와 실제 파일 형식이 다릅니다.")
    }
    let sections = archive.entries.enumerated().compactMap { index, entry -> (Int, Int, ZipEntry)? in
        guard entry.name.hasPrefix("Contents/section"), entry.name.hasSuffix(".xml") else { return nil }
        let digits = entry.name.dropFirst(16).dropLast(4)
        guard !digits.isEmpty, digits.allSatisfy(\.isASCII), let number = Int(digits) else { return nil }
        return (number, index, entry)
    }.sorted { ($0.0, $0.1) < ($1.0, $1.1) }.map(\.2)
    // Bounded before inflating, so a zip bomb never reaches memory.
    if sections.reduce(0, { $0 + $1.size }) > maxOutput { throw .rejected("한글 문서 내용이 너무 큽니다.") }
    var lines: [String] = []
    for section in sections {
        let xml = try archive.read(section)
        // Entities could inflate past the size bound.
        if Data(xml).range(of: Data("<!DOCTYPE".utf8)) != nil { throw unreadable }
        let builder = TreeBuilder()
        let parser = XMLParser(data: Data(xml))
        parser.shouldProcessNamespaces = true
        parser.shouldResolveExternalEntities = false
        parser.delegate = builder
        guard parser.parse(), let root = builder.root else { throw unreadable }
        root.walk { paragraph in
            guard paragraph.is("p") else { return }
            lines.append(paragraph.children("run").flatMap { $0.children("t") }.map(\.allText).joined())
        }
    }
    let text = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    if text.isEmpty { throw .rejected("한글 문서에 글자가 없습니다.") }
    return Data((text + "\n").utf8)
}

// MARK: - XML (a small ElementTree: text and child elements in document order)

final class XMLNode {
    let uri: String?
    let name: String
    var items: [Item] = []
    enum Item { case text(String), node(XMLNode) }
    init(uri: String?, name: String) { (self.uri, self.name) = (uri, name) }

    func `is`(_ local: String) -> Bool { uri == hwpxParagraph && name == local }
    func children(_ local: String) -> [XMLNode] {
        items.compactMap { if case let .node(n) = $0, n.is(local) { return n }; return nil }
    }
    /// ElementTree `itertext()`.
    var allText: String {
        items.map { switch $0 { case let .text(t): t; case let .node(n): n.allText } }.joined()
    }
    /// ElementTree `iter()`: this element, then its descendants in document order.
    func walk(_ visit: (XMLNode) -> Void) {
        visit(self)
        for case let .node(n) in items { n.walk(visit) }
    }
}

final class TreeBuilder: NSObject, XMLParserDelegate {
    var root: XMLNode?
    private var stack: [XMLNode] = []

    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName: String?, attributes: [String: String] = [:]) {
        let node = XMLNode(uri: namespaceURI, name: elementName)
        if let parent = stack.last { parent.items.append(.node(node)) } else { root = node }
        stack.append(node)
    }

    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?,
                qualifiedName: String?) {
        stack.removeLast()
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        stack.last?.items.append(.text(string))
    }

    func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) {
        stack.last?.items.append(.text(String(decoding: CDATABlock, as: UTF8.self)))
    }
}

// MARK: - Zip (read-only, no zip64 or encryption; as strict as Python zipfile on what it reads)

struct ZipEntry {
    let raw: [UInt8]
    var name: String { String(decoding: raw, as: UTF8.self) }
    let method: Int
    let flags: Int
    let crc: UInt32
    let compressedSize: Int
    let size: Int
    let headerOffset: Int
}

// ponytail: no zip64 (archives over 4 GiB or 65535 entries) — HWPX files are far smaller.
struct ZipArchive {
    let bytes: [UInt8]
    var entries: [ZipEntry] = []

    init(_ bytes: [UInt8]) throws(ToolError) {
        self.bytes = bytes
        // End of central directory: the last 22 bytes, or within a trailing comment of up to 64 KiB.
        guard bytes.count >= 22 else { throw unreadable }
        var end = bytes.count - 22
        let floor = max(0, bytes.count - 22 - 65_535)
        while end >= floor, u32(end) != 0x0605_4B50 { end -= 1 }
        guard end >= floor else { throw unreadable }
        let count = u16(end + 10), size = u32(end + 12), offset = u32(end + 16)
        if count == 0xFFFF || size == 0xFFFF_FFFF || offset == 0xFFFF_FFFF { throw unreadable }
        let start = end - Int(size)
        let concat = start - Int(offset)  // bytes prepended to the archive (Python does the same)
        guard start >= 0, concat >= 0 else { throw unreadable }
        var at = start
        for _ in 0..<count {
            guard at + 46 <= end, u32(at) == 0x0201_4B50 else { throw unreadable }
            let nameLength = u16(at + 28), extra = u16(at + 30), comment = u16(at + 32)
            guard at + 46 + nameLength <= end else { throw unreadable }
            entries.append(ZipEntry(
                raw: Array(bytes[at + 46..<at + 46 + nameLength]),
                method: u16(at + 10), flags: u16(at + 8), crc: UInt32(u32(at + 16)),
                compressedSize: u32(at + 20), size: u32(at + 24), headerOffset: u32(at + 42) + concat))
            at += 46 + nameLength + extra + comment
        }
    }

    private func u16(_ at: Int) -> Int { Int(bytes[at]) | Int(bytes[at + 1]) << 8 }
    private func u32(_ at: Int) -> Int { u16(at) | u16(at + 2) << 16 }

    func read(_ entry: ZipEntry) throws(ToolError) -> [UInt8] {
        let at = entry.headerOffset
        guard at >= 0, at + 30 <= bytes.count, u32(at) == 0x0403_4B50 else { throw unreadable }
        let nameLength = u16(at + 26), start = at + 30 + nameLength + u16(at + 28)
        guard start + entry.compressedSize <= bytes.count,
              Array(bytes[at + 30..<at + 30 + nameLength]) == entry.raw,
              entry.flags & 1 == 0
        else { throw unreadable }
        let packed = bytes[start..<start + entry.compressedSize]
        var out: [UInt8]
        switch entry.method {
        case 0:
            out = Array(packed)
        case 8:
            if packed.isEmpty, entry.size > 0 { throw unreadable }
            out = [UInt8](repeating: 0, count: entry.size)
            let written = entry.size == 0 ? 0 : packed.withUnsafeBufferPointer { source in
                out.withUnsafeMutableBufferPointer { target in
                    compression_decode_buffer(target.baseAddress!, entry.size, source.baseAddress!,
                                              source.count, nil, COMPRESSION_ZLIB)
                }
            }
            if written != entry.size { throw unreadable }
        default:
            throw unreadable
        }
        guard out.count == entry.size, crc32(out) == entry.crc else { throw unreadable }
        return out
    }
}

private let crcTable: [UInt32] = (0..<256).map { n in
    (0..<8).reduce(UInt32(n)) { c, _ in c & 1 != 0 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
}

func crc32(_ bytes: [UInt8]) -> UInt32 {
    ~bytes.reduce(~UInt32(0)) { crcTable[Int(($0 ^ UInt32($1)) & 0xFF)] ^ ($0 >> 8) }
}
