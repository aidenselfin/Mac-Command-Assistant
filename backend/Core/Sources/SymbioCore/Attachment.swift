import Foundation

/// Python `attachment.py`: a file the user dropped on the window, checked before it becomes
/// a command's target. A file outside the allowed folders, a link, a folder or a missing file
/// is refused, and the command is not sent at all.

/// The reason a dropped file was refused, written for the user.
public struct AttachmentError: Error, Equatable, Sendable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

/// The canonical path of one regular file inside an allowed folder, else AttachmentError.
public func checkAttachment(_ raw: JSON?, roots: [URL]) throws(AttachmentError) -> String {
    guard let text = raw?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
        throw AttachmentError("첨부할 파일 경로가 올바르지 않습니다.")
    }
    let resolvedRoots = roots.map { URL(fileURLWithPath: realPath($0.path) ?? normalized($0.path), isDirectory: true) }
    let path: URL
    do {
        path = try inside(.string(text), roots: resolvedRoots, exists: true)
    } catch {
        throw AttachmentError(
            "허용 폴더 밖이거나 링크, 없는 파일이라 첨부할 수 없어요. 요청을 거부했습니다. "
                + "허용 폴더: " + resolvedRoots.map(\.path).joined(separator: ", "))
    }
    var info = stat()
    guard stat(path.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else {
        throw AttachmentError("폴더나 특수 파일은 첨부할 수 없어요. 파일 하나만 놓아 주세요.")
    }
    return normalized(text)
}
